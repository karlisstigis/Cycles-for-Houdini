# SPDX-License-Identifier: Apache-2.0
"""Pure SDK provider policy checks; no renderer or Houdini process is launched."""

from support.paths import artifacts_root
import argparse
import hashlib
import io
from support.package_loader import import_product
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
S = import_product("sdk_dependencies")
POLICY = json.loads((ROOT / "source.lock.json").read_text())["sdk_dependencies"]["openimagedenoise"]


class PolicyTests(unittest.TestCase):
    def setUp(self):
        directory = artifacts_root() / "sdk-dependency-unit"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hfs = self.root / "sdk"
        headers = self.hfs / "toolkit/include"
        (headers / "SYS").mkdir(parents=True)
        (headers / "SYS/SYS_Version.h").write_text(f'#define SYS_VERSION_FULL "{POLICY["houdini"]}"\n')
        (headers / "UT").mkdir()
        (headers / "UT/UT_HDKVersion.h").write_text("#define HDK_API_VERSION 22000453\n")
        (headers / "OpenImageDenoise").mkdir()
        (headers / "OpenImageDenoise/config.h").write_text('#define OIDN_VERSION_STRING "2.3.3"\n')
        for name in ("oidn.h", "oidn.hpp"):
            (headers / "OpenImageDenoise" / name).write_text("header fixture " + name)
        (self.hfs / "custom/houdini/dsolib").mkdir(parents=True)
        (self.hfs / "custom/houdini/dsolib/OpenImageDenoise.lib").write_bytes(b"import fixture")
        (self.hfs / "bin").mkdir()
        for name in POLICY["runtime_files"]:
            (self.hfs / "bin" / name).write_bytes(("runtime fixture " + name).encode())
        self.version = patch.object(S, "file_version", return_value="2.3.3.0")
        self.version.start()
        self.addCleanup(self.version.stop)

    def record(self):
        return S.inspect_sdk(self.hfs, POLICY)

    def test_sdk_identity_is_relative_and_complete(self):
        record = self.record()
        self.assertEqual(len(record["files"]), 8)
        self.assertFalse(record["redistributed"])
        self.assertNotIn(str(self.hfs), json.dumps(record))
        self.assertEqual(
            S.validate_modules([self.hfs / "bin/OpenImageDenoise.dll"], self.hfs, record)[0]["version"], "2.3.3.0"
        )

    def test_matching_sdk_relocates(self):
        record = self.record()
        relocated = self.root / "different location/sdk"
        shutil.copytree(self.hfs, relocated)
        self.assertEqual(record, S.inspect_sdk(relocated, POLICY))
        S.validate_modules([relocated / "bin/OpenImageDenoise.dll"], relocated, record)

    def test_wrong_header_fails(self):
        (self.hfs / "toolkit/include/OpenImageDenoise/config.h").write_text('#define OIDN_VERSION_STRING "2.5.0"')
        with self.assertRaisesRegex(RuntimeError, "unsupported OIDN headers"):
            self.record()

    def test_wrong_host_fails(self):
        (self.hfs / "toolkit/include/SYS/SYS_Version.h").write_text('#define SYS_VERSION_FULL "22.1.430"')
        with self.assertRaisesRegex(RuntimeError, "unsupported Houdini"):
            self.record()

    def test_edition_check_only_rejects_apprentice_licenses(self):
        hou = MagicMock()
        with patch.dict(sys.modules, hou=hou):
            for edition in ("Apprentice", "ApprenticeHD", "Indie", "Commercial"):
                with self.subTest(edition=edition):
                    hou.licenseCategory.return_value = getattr(hou.licenseCategoryType, edition)
                    if edition.startswith("Apprentice"):
                        with self.assertRaisesRegex(S.SDKDependencyError, "Indie, Core or FX"):
                            S.check_houdini_edition()
                    else:
                        S.check_houdini_edition()

    def test_compatible_sdk_build_records_its_own_hdk_api(self):
        (self.hfs / "toolkit/include/SYS/SYS_Version.h").write_text('#define SYS_VERSION_FULL "22.0.429"')
        (self.hfs / "toolkit/include/UT/UT_HDKVersion.h").write_text("#define HDK_API_VERSION 22000425\n")
        record = self.record()
        self.assertEqual((record["houdini"], record["hdk_api"]), ("22.0.429", 22000425))

    def test_load_compatibility_requires_matching_hdk_api_and_supports_strict_mode(self):
        self.assertTrue(S.check_host_compatibility("22.0.466", 22000453, "22.0.467", 22000453))
        self.assertFalse(S.check_host_compatibility("22.0.466", 22000453, "22.0.466", 22000453, True))
        with self.assertRaisesRegex(S.SDKDependencyError, "strict build matching"):
            S.check_host_compatibility("22.0.466", 22000453, "22.0.467", 22000453, True)
        with self.assertRaisesRegex(S.SDKDependencyError, "incompatible HDK API"):
            S.check_host_compatibility("22.0.466", 22000453, "22.0.429", 22000425)
        with self.assertRaisesRegex(S.SDKDependencyError, "unsupported Houdini"):
            S.check_host_compatibility("22.0.466", 22000453, "22.1.466", 22000453)

    def test_compatible_build_warning_reaches_log_and_gui_once(self):
        hou = MagicMock()
        hou.isUIAvailable.return_value = True
        log = io.StringIO()
        with (
            patch.object(S, "_compatibility_warning_sent", False),
            patch.dict(sys.modules, hou=hou),
            patch.object(S.sys, "stderr", log),
        ):
            S.warn_compatible_build("22.0.466", "22.0.467")
            S.warn_compatible_build("22.0.466", "22.0.467")
        self.assertIn("built for 22.0.466; running compatible Houdini 22.0.467", log.getvalue())
        self.assertEqual(log.getvalue().count("Cycles for Houdini:"), 1)
        hou.ui.setStatusMessage.assert_called_once()

    def test_wrong_runtime_version_fails(self):
        with patch.object(S, "file_version", return_value="2.5.0.0"):
            with self.assertRaisesRegex(RuntimeError, "wrong OIDN runtime version"):
                self.record()

    def test_missing_runtime_fails(self):
        (self.hfs / "bin/OpenImageDenoise_device_cuda.dll").unlink()
        with self.assertRaisesRegex(RuntimeError, "missing SDK dependency"):
            self.record()

    def test_preloaded_foreign_module_fails(self):
        record = self.record()
        other = self.root / "vendor/OpenImageDenoise.dll"
        other.parent.mkdir()
        other.write_bytes(b"foreign runtime")
        with self.assertRaisesRegex(RuntimeError, "unexpected/preloaded"):
            S.validate_modules([self.hfs / "bin/OpenImageDenoise.dll", other], self.hfs, record)

    def test_modified_sdk_bytes_fail(self):
        record = self.record()
        (self.hfs / "bin/OpenImageDenoise.dll").write_bytes(b"changed SDK runtime")
        with self.assertRaisesRegex(RuntimeError, "differs from the built SDK"):
            S.validate_modules([self.hfs / "bin/OpenImageDenoise.dll"], self.hfs, record)

    def test_missing_loaded_main_fails(self):
        with self.assertRaisesRegex(RuntimeError, "not loaded"):
            S.validate_modules([], self.hfs, self.record())

    def test_vendor_cache_and_static_components_fail(self):
        cache = self.root / "CMakeCache.txt"
        values = dict(
            CH_OIDN_PROVIDER="houdini-sdk",
            OPENIMAGEDENOISE_INCLUDE_DIR=str(self.hfs / "toolkit/include"),
            OPENIMAGEDENOISE_LIBRARY=str(self.hfs / "custom/houdini/dsolib/OpenImageDenoise.lib"),
            OPENIMAGEDENOISE_OPENIMAGEDENOISE_LIBRARY=str(self.hfs / "custom/houdini/dsolib/OpenImageDenoise.lib"),
        )

        def write():
            cache.write_text("\n".join(name + ":STRING=" + value for name, value in values.items()))

        write()
        S.validate_cache(cache, self.hfs)
        values["OPENIMAGEDENOISE_DNNL_LIBRARY"] = str(self.root / "vendor/dnnl.lib")
        write()
        with self.assertRaisesRegex(RuntimeError, "competing static"):
            S.validate_cache(cache, self.hfs)
        del values["OPENIMAGEDENOISE_DNNL_LIBRARY"]
        values["OPENIMAGEDENOISE_INCLUDE_DIR"] = str(self.root / "vendor/include")
        write()
        with self.assertRaisesRegex(RuntimeError, "pinned SDK path"):
            S.validate_cache(cache, self.hfs)

    def test_child_path_does_not_mutate_parent(self):
        with patch.dict(os.environ, HFS=str(self.hfs), PATH="existing-one" + os.pathsep + str(self.hfs / "bin")):
            before = dict(os.environ)
            child = S.worker_environment()
            self.assertEqual(child["PATH"].split(os.pathsep), [str(self.hfs / "bin"), "existing-one"])
            self.assertEqual(before, dict(os.environ))

    def test_runtime_policy_uses_active_hfs(self):
        host = self.root / "host"
        (host / "resources").mkdir(parents=True)
        (host / "resources/sdk-dependencies.json").write_text(json.dumps(dict(openimagedenoise=self.record())))
        relocated = self.root / "relocated sdk"
        shutil.copytree(self.hfs, relocated)
        with (
            patch.dict(os.environ, HFS=str(relocated), CYCLES_HOUDINI_HOST_ROOT=str(host)),
            patch.object(S, "loaded_modules", return_value=[relocated / "bin/OpenImageDenoise.dll"]),
        ):
            value = S.validate_runtime(require_loaded=True)
            self.assertEqual(value["version"], "2.3.3")
            self.assertTrue(Path(value["loaded"][0]["path"]).is_relative_to(relocated))

    def test_compatible_build_warns_once_after_runtime_checks(self):
        host = self.root / "host"
        (host / "resources").mkdir(parents=True)
        (host / "resources/sdk-dependencies.json").write_text(json.dumps(dict(openimagedenoise=self.record())))
        (self.hfs / "toolkit/include/SYS/SYS_Version.h").write_text('#define SYS_VERSION_FULL "22.0.467"')
        with (
            patch.dict(os.environ, HFS=str(self.hfs), CYCLES_HOUDINI_HOST_ROOT=str(host)),
            patch.object(S, "loaded_modules", return_value=[self.hfs / "bin/OpenImageDenoise.dll"]),
            patch.object(S, "warn_compatible_build") as warn,
        ):
            S.validate_runtime()
            warn.assert_called_once_with(POLICY["houdini"], "22.0.467")

    def test_missing_or_malformed_runtime_metadata_has_typed_error(self):
        host = self.root / "host"
        (host / "resources").mkdir(parents=True)
        metadata = host / "resources/sdk-dependencies.json"
        with patch.dict(os.environ, HFS=str(self.hfs), CYCLES_HOUDINI_HOST_ROOT=str(host)):
            for content in (None, "{broken", "[]", "{}", '{"openimagedenoise":{}}'):
                if content is not None:
                    metadata.write_text(content)
                with self.assertRaises(S.SDKDependencyError):
                    S.validate_runtime()

    def test_programming_errors_are_not_normalized(self):
        with patch.object(S, "_validate_runtime", side_effect=ValueError("programming error")):
            with self.assertRaisesRegex(ValueError, "programming error"):
                S.validate_runtime()

    def test_worker_is_verified_before_configuration(self):
        process = type("Process", (), dict(pid=123, poll=lambda self: None))()
        expected = dict(provider="houdini-sdk", loaded=[dict(path=str(self.hfs / "bin/OpenImageDenoise.dll"))])
        with (
            patch.object(S, "validate_runtime", side_effect=[dict(loaded=[]), expected]) as validate,
            patch.object(S.time, "sleep"),
        ):
            self.assertEqual(S.wait_for_worker_provider(process), expected)
            self.assertEqual(validate.call_count, 2)

    def test_exited_worker_fails_without_configuration(self):
        process = type("Process", (), dict(pid=123, returncode=17, poll=lambda self: 17))()
        with self.assertRaisesRegex(S.SDKDependencyError, "exit 17"):
            S.wait_for_worker_provider(process)

    def test_transient_worker_module_enumeration_recovers(self):
        process = type("Process", (), dict(pid=123, poll=lambda self: None))()
        expected = dict(loaded=[dict(path=str(self.hfs / "bin/OpenImageDenoise.dll"))])
        with (
            patch.object(
                S, "validate_runtime", side_effect=[S.SDKModuleEnumerationUnavailable("loader pending"), expected]
            ),
            patch.object(S.time, "sleep"),
        ):
            self.assertEqual(S.wait_for_worker_provider(process), expected)

    def test_permanent_worker_enumeration_failure_times_out(self):
        process = type("Process", (), dict(pid=123, poll=lambda self: None))()
        with (
            patch.object(S, "validate_runtime", side_effect=S.SDKModuleEnumerationUnavailable("loader pending")),
            patch.object(S.time, "monotonic", side_effect=[0.0, 1.0]),
        ):
            with self.assertRaisesRegex(S.SDKDependencyError, "did not load"):
                S.wait_for_worker_provider(process, timeout=0.5)

    def test_worker_foreign_provider_is_rejected_immediately(self):
        process = type("Process", (), dict(pid=123, poll=lambda self: None))()
        with patch.object(S, "validate_runtime", side_effect=S.SDKDependencyError("unexpected/preloaded")) as validate:
            with self.assertRaisesRegex(S.SDKDependencyError, "unexpected/preloaded"):
                S.wait_for_worker_provider(process)
            self.assertEqual(validate.call_count, 1)

    def test_unnameable_module_prevents_complete_attestation(self):
        kernel, psapi = MagicMock(), MagicMock()
        kernel.OpenProcess.return_value = 1

        def enumerate_modules(handle, buffer, size, needed, flags):
            needed._obj.value = S.ctypes.sizeof(S.wintypes.HMODULE)
            buffer[0] = 1
            return 1

        psapi.EnumProcessModulesEx.side_effect = enumerate_modules
        for returned_length in (0, 32768):
            psapi.GetModuleFileNameExW.return_value = returned_length
            with patch.object(S.ctypes, "WinDLL", side_effect=[kernel, psapi], create=True):
                with self.assertRaisesRegex(S.SDKModuleEnumerationUnavailable, "identify every"):
                    S.loaded_modules()
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_unobservable_process_is_typed(self):
        kernel, psapi = MagicMock(), MagicMock()
        kernel.OpenProcess.return_value = 0
        with patch.object(S.ctypes, "WinDLL", side_effect=[kernel, psapi], create=True):
            with self.assertRaisesRegex(S.SDKModuleEnumerationUnavailable, "cannot observe"):
                S.loaded_modules()
        kernel.CloseHandle.assert_not_called()

    def test_host_module_observation_retries_only_typed_unavailability(self):
        expected = [self.hfs / "bin/OpenImageDenoise.dll"]
        with (
            patch.object(S, "loaded_modules", side_effect=[S.SDKModuleEnumerationUnavailable("unnameable"), expected]),
            patch.object(S.time, "sleep"),
        ):
            self.assertEqual(S.observe_host_modules(), expected)
        with (
            patch.object(S, "loaded_modules", side_effect=S.SDKModuleEnumerationUnavailable("unnameable")),
            patch.object(S.time, "monotonic", side_effect=[0.0, 1.0]),
        ):
            with self.assertRaisesRegex(S.SDKModuleEnumerationUnavailable, "unnameable"):
                S.observe_host_modules(timeout=0.5)
        with patch.object(S, "loaded_modules", side_effect=S.SDKDependencyError("foreign provider")) as observe:
            with self.assertRaisesRegex(S.SDKDependencyError, "foreign provider"):
                S.observe_host_modules()
            self.assertEqual(observe.call_count, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report", type=Path, default=artifacts_root() / "sdk-dependency-policy-unit.json")
    args, remaining = parser.parse_known_args()
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    files = [
        "tests/sdk_dependency_policy_test.py",
        "houdini/python3.13libs/cycles_houdini/core/sdk_dependencies.py",
        "scripts/sdk-dependencies.py",
        "cmake/houdini-sdk-oidn.cmake",
        "source.lock.json",
    ]
    args.report.write_text(
        json.dumps(
            dict(
                schema_version=1,
                result="passed" if program.result.wasSuccessful() else "failed",
                tests_run=program.result.testsRun,
                failures=len(program.result.failures),
                errors=len(program.result.errors),
                scope=__doc__,
                source_files=[
                    dict(path=name, sha256=hashlib.sha256((ROOT / name).read_bytes()).hexdigest()) for name in files
                ],
            ),
            indent=2,
        )
    )
    raise SystemExit(not program.result.wasSuccessful())
