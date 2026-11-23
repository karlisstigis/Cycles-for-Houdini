# SPDX-License-Identifier: Apache-2.0
"""Portable packaging tests. No Houdini import, renderer, or real preferences."""

from support.paths import artifacts_root

import importlib.util
import argparse
import base64
from contextlib import redirect_stdout
import io
import json
import os
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location(
    "release_package", Path(__file__).parents[1] / "scripts/release-package.py"
)
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


def pe_binary(imports=(), delayed=()):
    """Independent tiny x64 PE fixture with imports and RVA delay imports."""
    data = bytearray(8192)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HH", data, 132, 0x8664, 1)
    struct.pack_into("<H", data, 148, 240)
    optional = 152
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<Q", data, optional + 24, 0x140000000)
    struct.pack_into("<I", data, optional + 60, 512)
    struct.pack_into("<I", data, optional + 108, 16)
    section = optional + 240
    struct.pack_into("<4I", data, section + 8, 7680, 4096, 7680, 512)
    string_at = 2048
    for directory, values, offset, stride, name_offset in ((1, imports, 512, 20, 12), (13, delayed, 1024, 32, 4)):
        if not values:
            continue
        struct.pack_into("<2I", data, optional + 112 + directory * 8, 4096 + offset - 512, (len(values) + 1) * stride)
        for n, value in enumerate(values):
            entry = offset + n * stride
            if directory == 13:
                struct.pack_into("<I", data, entry, 1)
            struct.pack_into("<I", data, entry + name_offset, 4096 + string_at - 512)
            encoded = value.encode("ascii") + b"\0"
            data[string_at : string_at + len(encoded)] = encoded
            string_at += len(encoded)
    return bytes(data)


def fixture_archive(
    path,
    payload=b"native fixture",
    purpose="internal",
    houdini="22.0.429",
    provider=None,
    asset_browser=False,
    extra_path=None,
):
    files = {
        "host/bin/ch_worker.exe": payload,
        "host/dso/_cycles_houdini.dll": b"host",
        "host/resources/lib/kernel.ptx": b"kernel",
        "runtime/bin/embree4.dll": b"dependency",
    }
    if extra_path:
        files[extra_path] = b"budget fixture"
    if asset_browser:
        files["host/addons/asset_browser/python_panels/cycles_assets.pypanel"] = b"<pythonPanelDocument/>"
    if provider is not None:
        files["host/resources/sdk-dependencies.json"] = P.canonical(dict(openimagedenoise=provider))
    manifest = dict(
        schema_version=P.SCHEMA,
        platform="windows-x64",
        product="Cycles for Houdini",
        purpose=purpose,
        houdini=houdini,
        runtime_fingerprint="a" * 64,
        audit={"build_sources": "passed", "dependencies": "passed", "notices": "passed"},
        certification="passed" if purpose == "public" else "internal candidate",
        files=[dict(path=name, bytes=len(value), sha256=P.digest(value)) for name, value in sorted(files.items())],
    )
    manifest["package_id"] = P.digest(P.canonical(manifest))
    files[P.MANIFEST] = P.canonical(manifest)
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return manifest


class ReleaseTests(unittest.TestCase):
    def test_package_eligibility_checks_hdk_before_exposing_dso(self):
        with tempfile.TemporaryDirectory() as temp:
            sdk_root = Path(temp)
            for version, api in (
                ("22.0.429", 22000425),
                ("22.0.466", 22000453),
                ("22.0.467", 22000453),
                ("22.1.1", 22000453),
            ):
                include = sdk_root / ("Houdini " + version) / "toolkit/include"
                (include / "SYS").mkdir(parents=True)
                (include / "UT").mkdir()
                (include / "SYS/SYS_Version.h").write_text(f'#define SYS_VERSION_FULL "{version}"\n')
                (include / "UT/UT_HDKVersion.h").write_text(f"#define HDK_API_VERSION {api}\n")
            eligible = P.package_enable("22.0.466", 22000453, sdk_root)
            self.assertEqual(
                eligible,
                "houdini_version == '22.0.466' or (houdini_version == '22.0.467' and $CYCLES_HOUDINI_STRICT_BUILD != '1')",
            )
            self.assertNotIn("22.0.429", eligible)
            self.assertNotIn("22.1.1", eligible)
            self.assertEqual(P.package_eligible_versions(eligible), {"22.0.466", "22.0.467"})
            self.assertFalse(P.eligible_sets_overlap(P.package_eligible_versions(eligible), {"22.0.429"}))
            self.assertTrue(P.eligible_sets_overlap(P.package_eligible_versions(eligible), {"22.0.467"}))
            with self.assertRaisesRegex(P.PackageError, "Unparseable"):
                P.package_eligible_versions("houdini_version >= '22.0'")
            for version in ("22.0.466", "22.0.467"):
                runtime = sdk_root / ("Houdini " + version) / "bin/OpenImageDenoise.dll"
                runtime.parent.mkdir()
                runtime.write_bytes(b"matching SDK runtime")
            provider = dict(
                files=[
                    dict(
                        path="bin/OpenImageDenoise.dll",
                        kind="runtime",
                        bytes=len(b"matching SDK runtime"),
                        sha256=P.digest(b"matching SDK runtime"),
                    )
                ]
            )
            self.assertIn("22.0.467", P.package_enable("22.0.466", 22000453, sdk_root, provider))
            (sdk_root / "Houdini 22.0.467/bin/OpenImageDenoise.dll").write_bytes(b"different SDK runtime")
            self.assertNotIn("22.0.467", P.package_enable("22.0.466", 22000453, sdk_root, provider))
            self.assertEqual(
                P.package_enable("22.0.466", 22000453, sdk_root / "absent"), "houdini_version == '22.0.466'"
            )

    def test_refresh_registration_uses_owned_receipt_and_explicit_sdk_path(self):
        dll = b"frozen SDK runtime"
        provider = dict(
            hdk_api=22000425,
            files=[dict(path="bin/OpenImageDenoise.dll", kind="runtime", bytes=len(dll), sha256=P.digest(dll))],
        )
        fixture_archive(self.archive, provider=provider)
        program_files = self.base / "Program Files"
        installed = program_files / "Side Effects Software"

        def sdk(root, version, api, runtime=dll):
            include = root / "toolkit/include"
            (include / "SYS").mkdir(parents=True)
            (include / "UT").mkdir()
            (include / "SYS/SYS_Version.h").write_text(f'#define SYS_VERSION_FULL "{version}"\n')
            (include / "UT/UT_HDKVersion.h").write_text(f"#define HDK_API_VERSION {api}\n")
            (root / "bin").mkdir()
            (root / "bin/OpenImageDenoise.dll").write_bytes(runtime)

        sdk(installed / "Houdini 22.0.429", "22.0.429", 22000425)
        with patch.dict(os.environ, {"ProgramFiles": str(program_files)}):
            self.install()
            original = P.read_json(self.root / "state.json")
            path = Path(original["package_path"])
            self.assertEqual(P.refresh_registration(self.root)["changed"], False)
            sdk(installed / "Houdini 22.0.430", "22.0.430", 22000425)
            refreshed = P.refresh_registration(self.root)
            self.assertTrue(refreshed["changed"])
            self.assertEqual(refreshed["eligible_versions"], ["22.0.429", "22.0.430"])
            self.assertEqual(P.read_json(self.root / "state.json")["active"], original["active"])
            self.assertEqual(P.read_json(self.root / "state.json")["archive_sha256"], original["archive_sha256"])
            self.assertIn("$CYCLES_HOUDINI_STRICT_BUILD != '1'", P.read_json(path)["enable"])
            outside = self.base / "nondefault/Houdini 22.0.431"
            sdk(outside, "22.0.431", 22000425)
            self.assertIn("22.0.431", P.refresh_registration(self.root, [outside])["eligible_versions"])
            outside_b = self.base / "another/Houdini 22.0.433"
            sdk(outside_b, "22.0.433", 22000425)
            added_b = P.refresh_registration(self.root, [outside_b])
            self.assertEqual(added_b["eligible_versions"], ["22.0.429", "22.0.430", "22.0.431", "22.0.433"])
            self.assertEqual(added_b["registered_houdini_roots"], sorted([str(outside), str(outside_b)]))
            self.assertFalse(P.refresh_registration(self.root)["changed"])
            self.install()
            self.assertEqual(P.refresh_registration(self.root)["eligible_versions"], added_b["eligible_versions"])
            before = path.read_bytes()
            sdk(self.base / "wrong/Houdini 22.0.466", "22.0.466", 22000453)
            with self.assertRaisesRegex(P.PackageError, "HDK API 22000453 differs"):
                P.refresh_registration(self.root, [self.base / "wrong/Houdini 22.0.466"])
            self.assertEqual(path.read_bytes(), before)
            sdk(self.base / "wrong/Houdini 22.0.432", "22.0.432", 22000425, b"different")
            with self.assertRaisesRegex(P.PackageError, "SDK DLL identity differs"):
                P.refresh_registration(self.root, [self.base / "wrong/Houdini 22.0.432"])
            self.assertEqual(path.read_bytes(), before)
            competing = path.parent / "cycles_houdini_competing.json"
            P.atomic_json(competing, dict(enable="houdini_version == '22.0.431'"))
            with self.assertRaisesRegex(P.PackageError, "eligible for the same"):
                P.refresh_registration(self.root, [outside])
            self.assertEqual(path.read_bytes(), before)
            P.atomic_json(competing, dict(enable="houdini_version >= '22.0'"))
            with self.assertRaisesRegex(P.PackageError, "Unparseable Cycles package eligibility"):
                P.refresh_registration(self.root)
            competing.unlink()
            shutil.rmtree(outside)
            after_removed = P.refresh_registration(self.root)
            self.assertTrue(after_removed["changed"])
            self.assertEqual(after_removed["registered_houdini_roots"], [str(outside_b)])
            self.assertEqual(after_removed["removed_houdini_roots"][0]["path"], str(outside))
            (outside_b / "bin/OpenImageDenoise.dll").write_bytes(b"different SDK runtime")
            after_changed = P.refresh_registration(self.root)
            self.assertTrue(after_changed["changed"])
            self.assertEqual(after_changed["registered_houdini_roots"], [])
            self.assertEqual(after_changed["eligible_versions"], ["22.0.429", "22.0.430"])
            before = path.read_bytes()
            path.write_bytes(before + b" ")
            with self.assertRaisesRegex(P.PackageError, "changed externally"):
                P.refresh_registration(self.root)

    def test_native_and_solaris_payloads_are_separate(self):
        native = {"host/bin/ch_worker.exe": None, "host/dso/_cycles_houdini.dll": None}
        solaris = {**native, P.HYDRA_DESCRIPTOR: None, "host/dso/usd_plugins/hdCycles/hdCycles.dll": None}
        P.validate_solaris_payload({}, native)
        P.validate_solaris_payload({"WITH_CYCLES_HYDRA_RENDER_DELEGATE": "ON"}, solaris)
        for enabled, files in ((False, solaris), (True, native), (False, {**native, "tools/hdCycles.dll": None})):
            with self.subTest(enabled=enabled, files=files), self.assertRaises(P.PackageError):
                P.validate_solaris_payload({"WITH_CYCLES_HYDRA_RENDER_DELEGATE": "ON" if enabled else "OFF"}, files)

    def test_bridge_choice_is_per_install_and_survives_repair_refresh_and_rollback(self):
        # Component policy changes registration/receipt, never immutable files.
        # There is intentionally no Blender discovery in the installation tool.
        fixture_archive(self.archive, houdini="22.0.466", provider=dict(hdk_api=22000453, files=[]))
        original = b'{"enable":false,"artist":"preserve exact registration"}\r\n'
        package_path = self.prefs / "packages" / P.target_identity("22.0.466")["package_filename"]
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(original)

        def selection():
            state = P.read_json(self.root / "state.json")
            env = P.read_json(package_path)["env"]
            value = next(
                item["CYCLES_HOUDINI_BLENDER_BRIDGE"] for item in env if "CYCLES_HOUDINI_BLENDER_BRIDGE" in item
            )
            self.assertEqual(value, "1" if state["components"]["blender_bridge"] else "0")
            return state["components"]["blender_bridge"]

        self.install(blender_bridge=False, replace_existing=True)
        state = P.read_json(self.root / "state.json")
        version = P.version_dir(self.root, state["active"])
        files = {
            path.relative_to(version).as_posix(): path.read_bytes() for path in version.rglob("*") if path.is_file()
        }
        self.assertFalse(selection())
        self.install()  # Omitted selection means repair preserves the receipt.
        self.assertFalse(selection())
        with patch.object(
            P,
            "package_enable",
            return_value="houdini_version == '22.0.466' or (houdini_version == '22.0.467' and $CYCLES_HOUDINI_STRICT_BUILD != '1')",
        ):
            P.refresh_registration(self.root)
        self.assertFalse(selection())
        before = package_path.read_bytes()
        self.install(blender_bridge=True, defer_commit=True, setup_token="component-change")
        self.assertTrue(selection())
        P.abort_setup(self.root, "component-change")
        self.assertEqual(package_path.read_bytes(), before)
        self.assertFalse(selection())
        self.install(blender_bridge=True)
        self.assertTrue(selection())
        self.assertEqual(
            files,
            {path.relative_to(version).as_posix(): path.read_bytes() for path in version.rglob("*") if path.is_file()},
        )
        P.uninstall(self.root)
        self.assertEqual(package_path.read_bytes(), original)

    def test_legacy_receipt_keeps_bridge_and_invalid_choice_is_rejected(self):
        self.assertEqual(P.install_components({}), dict(blender_bridge=True, asset_browser=False))
        for value in ("false", 0, {}, None):
            with self.subTest(value=value), self.assertRaises(P.PackageError):
                P.install_components(dict(components=dict(blender_bridge=value)))

    def test_asset_browser_choice_defaults_off_and_depends_on_the_bridge(self):
        legacy = dict(components=dict(blender_bridge=True))
        self.assertEqual(P.install_components(legacy), dict(blender_bridge=True, asset_browser=False))
        chosen = dict(components=dict(blender_bridge=True, asset_browser=True))
        self.assertEqual(P.install_components(chosen), dict(blender_bridge=True, asset_browser=True))
        self.assertEqual(P.install_components(chosen, asset_browser=False)["asset_browser"], False)
        # Disabling the bridge disables a kept browser choice, and re-enabling the
        # bridge alone does not resurrect it.
        self.assertEqual(
            P.install_components(chosen, blender_bridge=False), dict(blender_bridge=False, asset_browser=False)
        )
        self.assertEqual(
            P.install_components(dict(components=dict(blender_bridge=False, asset_browser=False)), True)[
                "asset_browser"
            ],
            False,
        )
        for state, bridge in ((legacy, False), (dict(components=dict(blender_bridge=False)), None)):
            with (
                self.subTest(state=state, bridge=bridge),
                self.assertRaisesRegex(P.PackageError, "requires the Blender"),
            ):
                P.install_components(state, bridge, True)
        for value in ("true", 1, None, {}):
            with self.subTest(value=value), self.assertRaises(P.PackageError):
                P.install_components(dict(components=dict(blender_bridge=True, asset_browser=value)))
            with self.subTest(value=value), self.assertRaises(P.PackageError):
                P.install_components({}, None, value if value is not None else "yes")

    def test_asset_browser_choice_gates_hpath_and_survives_repair_and_refresh(self):
        fixture_archive(self.archive, houdini="22.0.466", provider=dict(hdk_api=22000453, files=[]), asset_browser=True)
        package_path = self.prefs / "packages" / P.target_identity("22.0.466")["package_filename"]

        def selection():
            state = P.read_json(self.root / "state.json")
            package = P.read_json(package_path)
            host = P.version_dir(self.root, state["active"]) / "host"
            env = {key: value for item in package["env"] for key, value in item.items()}
            enabled = state["components"]["asset_browser"]
            self.assertEqual(env["CYCLES_HOUDINI_ASSET_BROWSER"], "1" if enabled else "0")
            expected = [host.as_posix()] + ([(host / "addons/asset_browser").as_posix()] if enabled else [])
            self.assertEqual(package["hpath"], expected)
            self.assertEqual(
                list(package), ["enable", "env", "hpath", "load_package_once"], "package keys must stay stable"
            )
            self.assertEqual(
                [key for item in package["env"] for key in item][-3:],
                ["CYCLES_HOUDINI_BLENDER_BRIDGE", "CYCLES_HOUDINI_ASSET_BROWSER", "PATH"],
            )
            return state["components"]["blender_bridge"], enabled

        self.install()  # First install without an explicit choice: browser off.
        self.assertEqual(selection(), (True, False))
        self.install(asset_browser=True)
        self.assertEqual(selection(), (True, True))
        self.install()  # Repair keeps it.
        self.assertEqual(selection(), (True, True))
        with patch.object(
            P,
            "package_enable",
            return_value="houdini_version == '22.0.466' or (houdini_version == '22.0.467' and $CYCLES_HOUDINI_STRICT_BUILD != '1')",
        ):
            self.assertTrue(P.refresh_registration(self.root)["changed"])
        self.assertEqual(selection(), (True, True))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(P.main(["status", "--install-root", str(self.root)]), 0)
        self.assertEqual(json.loads(out.getvalue())["components"], dict(blender_bridge=True, asset_browser=True))
        before = package_path.read_bytes()
        with self.assertRaisesRegex(P.PackageError, "requires the Blender bridge"):
            self.install(blender_bridge=False, asset_browser=True)
        self.assertEqual(package_path.read_bytes(), before)
        self.install(blender_bridge=False)
        self.assertEqual(selection(), (False, False))
        self.install(blender_bridge=True)
        self.assertEqual(selection(), (True, False))
        self.install(asset_browser=True)
        self.install(asset_browser=False)
        self.assertEqual(selection(), (True, False))

    def test_asset_browser_needs_the_addon_in_the_payload(self):
        fixture_archive(self.archive, houdini="22.0.466", provider=dict(hdk_api=22000453, files=[]))
        with self.assertRaisesRegex(P.PackageError, "does not contain the asset browser"):
            self.install(asset_browser=True)
        state_path = self.root / "state.json"
        self.install()
        state = P.read_json(state_path)
        P.atomic_json(state_path, dict(state, components=dict(blender_bridge=True, asset_browser=True)))
        self.install()  # A kept choice is dropped for a payload without the add-on.
        self.assertFalse(P.read_json(state_path)["components"]["asset_browser"])

    def test_asset_browser_command_line(self):
        fixture_archive(self.archive, houdini="22.0.466", provider=dict(hdk_api=22000453, files=[]), asset_browser=True)
        common = [
            "install",
            str(self.archive),
            "--install-root",
            str(self.root),
            "--preference-root",
            str(self.prefs),
            "--data-root",
            str(self.data),
            "--allow-internal",
        ]
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(P.main(common + ["--blender-bridge", "enabled", "--asset-browser", "enabled"]), 0)
        self.assertEqual(json.loads(out.getvalue())["components"], dict(blender_bridge=True, asset_browser=True))
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), patch.object(sys, "stderr", error):
            self.assertEqual(P.main(common + ["--blender-bridge", "disabled", "--asset-browser", "enabled"]), 1)
        self.assertIn("requires the Blender bridge", error.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(P.main(common + ["--asset-browser", "disabled"]), 0)
        self.assertEqual(json.loads(out.getvalue())["components"], dict(blender_bridge=True, asset_browser=False))
        with self.assertRaises(SystemExit), patch.object(sys, "stderr", io.StringIO()):
            P.main(common + ["--asset-browser", "on"])

    def setUp(self):
        # Fixture installations must not depend on the artist's real Windows
        # registration. Individual ownership tests override this scoped default.
        registrations = patch.object(P, "windows_installations", return_value=[])
        registrations.start()
        self.addCleanup(registrations.stop)
        test_root = artifacts_root() / "packaging-unit"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="cycles-release-test-", dir=test_root)
        assert Path(self.temp.name).resolve().is_relative_to(test_root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.archive = self.base / "candidate.zip"
        self.manifest = fixture_archive(self.archive)
        self.root, self.prefs, self.data = (
            self.base / name for name in ("installation", "isolated-prefs", "runtime-data")
        )

    def install(self, **kwargs):
        return P.install(self.archive, self.root, self.prefs, self.data, allow_internal=True, **kwargs)

    @staticmethod
    def windows_powershell():
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]

    def windows_maintenance(self, action, app):
        script = Path(P.__file__).with_name("maintain-registration.ps1")
        identity = P.target_identity("22.0.429")
        return subprocess.run(
            [
                *self.windows_powershell(),
                "-File",
                str(script),
                "-Action",
                action,
                "-ApplicationRoot",
                str(app),
                "-AppId",
                identity["app_id"],
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_uninstalls_without_houdini_and_preserves_bytes(self):
        app = self.base / "App fixture \u00e4"
        prefs = self.base / "Prefs fixture \u00e9"
        pkg = prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        original = '{"enable": false, "title": "B\u00e9zier"}\r\n'.encode("utf-8-sig")
        pkg.parent.mkdir(parents=True)
        pkg.write_bytes(original)
        P.install(
            self.archive,
            app / "installation",
            prefs,
            app / "data",
            allow_internal=True,
            application_root=app,
            replace_existing=True,
        )
        installed = pkg.read_bytes()
        pkg.write_bytes(installed + b" ")
        bad = self.windows_maintenance("Uninstall", app)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("changed externally", bad.stdout)
        self.assertEqual(pkg.read_bytes(), installed + b" ")
        pkg.write_bytes(installed)
        for _ in range(2):
            result = self.windows_maintenance("Uninstall", app)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(pkg.read_bytes(), original)
        self.assertIsNone(P.read_json(app / "installation/state.json")["active"])
        self.assertEqual(P._pending(app / "installation"), [])

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_recovers_interrupted_install_then_uninstalls_absent_registration(self):
        app = self.base / "app"
        self.root = app / "installation"
        self.install(application_root=app, defer_commit=True, setup_token="fixture")
        pkg = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        self.assertTrue(pkg.exists())
        self.assertTrue(P._pending(self.root))
        result = self.windows_maintenance("Recover", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(pkg.exists())
        self.assertFalse((self.root / "state.json").exists())
        self.assertEqual(P._pending(self.root), [])
        self.install(application_root=app)
        self.assertTrue(pkg.exists())
        result = self.windows_maintenance("Uninstall", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(pkg.exists())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_refuses_partial_legacy_rollback_then_recovers(self):
        generic, original = self.legacy_install()
        old_package, old_state = generic.read_bytes(), (self.root / "state.json").read_bytes()
        app = self.base / "upgrade"
        root = app / "installation"
        exact = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        write = P.atomic_json

        def fail(path, value):
            if Path(path) == root / "state.json":
                raise OSError("interrupted legacy handover")
            write(path, value)

        def install(**kwargs):
            return P.install(
                self.archive, root, self.prefs, app / "data", allow_internal=True, application_root=app, **kwargs
            )

        with patch.object(P, "atomic_json", side_effect=fail), self.assertRaises(OSError):
            install()
        prepared = exact.read_bytes()
        retired = (self.root / "state.json").read_bytes()
        generic.write_bytes(b"external edit")
        refused = self.windows_maintenance("Recover", app)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("changed after interruption", refused.stdout)
        self.assertEqual(generic.read_bytes(), b"external edit")
        self.assertEqual(exact.read_bytes(), prepared)
        self.assertEqual((self.root / "state.json").read_bytes(), retired)
        generic.unlink()
        result = self.windows_maintenance("Recover", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(generic.read_bytes(), old_package)
        self.assertEqual((self.root / "state.json").read_bytes(), old_state)
        self.assertFalse(exact.exists())
        install()
        result = self.windows_maintenance("Uninstall", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(generic.read_bytes(), original)
        self.assertFalse(exact.exists())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_does_not_recreate_removed_preferences(self):
        app = self.base / "app"
        root = app / "installation"
        pkg = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        pkg.parent.mkdir(parents=True)
        pkg.write_bytes(b'{"enable":false,"artist":"unmanaged"}\r\n')
        P.install(
            self.archive,
            root,
            self.prefs,
            self.data,
            allow_internal=True,
            application_root=app,
            replace_existing=True,
        )
        moved = self.prefs.rename(self.base / "saved preferences")
        result = self.windows_maintenance("Uninstall", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.prefs.exists())
        self.assertIsNone(P.read_json(root / "state.json")["active"])
        self.assertTrue((moved / "packages" / pkg.name).exists())
        self.assertTrue((root / "versions").exists())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_removes_orphan_registration_after_payload_moves(self):
        app = self.base / "app"
        root = app / "installation"
        installed = P.install(self.archive, root, self.prefs, self.data, allow_internal=True, application_root=app)
        pkg = Path(installed["configuration"])
        unrelated = pkg.with_name("an_artist_package.json")
        unrelated.write_bytes(b'{"enable": false}\n')
        moved = (root / "versions").rename(root / "moved versions")
        result = self.windows_maintenance("Uninstall", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(pkg.exists())
        self.assertEqual(unrelated.read_bytes(), b'{"enable": false}\n')
        self.assertTrue(moved.exists())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_requires_pending_preferences_and_accepts_intact_legacy_marker(self):
        generic, _ = self.legacy_install()
        legacy_before = generic.read_bytes()
        result = self.windows_maintenance("Recover", self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(generic.read_bytes(), legacy_before)

        app = self.base / "app"
        root = app / "installation"
        P.install(
            self.archive,
            root,
            self.prefs,
            self.data,
            allow_internal=True,
            application_root=app,
            defer_commit=True,
            setup_token="fixture",
        )
        pending = P._pending(root)[0]
        before = pending.read_bytes()
        moved = self.prefs.rename(self.base / "saved preferences")
        failed = self.windows_maintenance("Uninstall", app)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("preferences folder is missing", failed.stdout)
        self.assertFalse(self.prefs.exists())
        self.assertEqual(pending.read_bytes(), before)
        moved.rename(self.prefs)
        result = self.windows_maintenance("Recover", app)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(generic.read_bytes(), legacy_before)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer helper")
    def test_windows_maintenance_serializes_against_python_and_running_setup(self):
        app = self.base / "app"
        self.root = app / "installation"
        self.install(application_root=app, defer_commit=True, setup_token="fixture")
        with P.installation_lock(self.prefs):
            refused = self.windows_maintenance("Recover", app)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("installation lock", refused.stdout)

        script = """
        $file = [IO.File]::Open($env:CYCLES_TEST_INSTALL_LOCK, 'Open', 'ReadWrite', 'ReadWrite')
        $file.Lock(0, 1)
        $mutex = [Threading.Mutex]::new($false, $env:CYCLES_TEST_INSTALL_MUTEX)
        $null = $mutex.WaitOne()
        [Console]::WriteLine('locked')
        $null = [Console]::ReadLine()
        $mutex.ReleaseMutex()
        $mutex.Dispose()
        $file.Dispose()
        """
        identity = P.target_identity("22.0.429")
        args = [*self.windows_powershell(), "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode()]
        env = dict(
            os.environ,
            CYCLES_TEST_INSTALL_LOCK=str(self.prefs / ".installation.lock"),
            CYCLES_TEST_INSTALL_MUTEX="Global\\CyclesForHoudiniInstaller-" + identity["app_id"],
        )
        with subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as child:
            try:
                self.assertEqual(child.stdout.readline(), "locked\n")
                with self.assertRaisesRegex(P.PackageError, "installation lock"):
                    P.recover(self.root)
                refused = self.windows_maintenance("Uninstall", app)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("already working", refused.stdout)
                self.assertTrue(P._pending(self.root))
            finally:
                _, err = child.communicate("\n", timeout=20)
                self.assertEqual(child.returncode, 0, err)
        retry = self.windows_maintenance("Recover", app)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(P._pending(self.root), [])

    def test_pe_normal_and_delayed_imports(self):
        path = self.base / "fixture.dll"
        path.write_bytes(pe_binary(("KERNEL32.DLL", "embree4.dll"), ("nvcuda.dll",)))
        self.assertEqual(P.pe_import_tables(path), ({"embree4.dll", "kernel32.dll"}, {"nvcuda.dll"}))
        self.assertEqual(P.pe_imports(path), ["embree4.dll", "kernel32.dll", "nvcuda.dll"])

    def test_pe_rejects_lfs_and_truncated_files(self):
        path = self.base / "bad.dll"
        for value in (b"version https://git-lfs.github.com/spec/v1", b"MZ", pe_binary()[:512]):
            path.write_bytes(value)
            with self.assertRaises(P.PackageError):
                P.pe_imports(path)

    def test_invalid_windows_paths(self):
        for value in ("../escape", "/root", "a/../b", "a\\b", "a:b", "CON", "lpt9.txt", "a.", "a ", "a//b", "a/\x01b"):
            with self.subTest(value=value), self.assertRaises(P.PackageError):
                P.relative_name(value)

    def test_archive_hash_and_tamper(self):
        self.assertEqual(P.verify_archive(self.archive)["package_id"], self.manifest["package_id"])
        with self.assertRaises(P.PackageError):
            P.verify_archive(self.archive, "0" * 64)
        altered = self.base / "altered.zip"
        with zipfile.ZipFile(self.archive) as original, zipfile.ZipFile(altered, "w") as output:
            for item in original.infolist():
                output.writestr(
                    item.filename, b"changed" if item.filename.endswith("ch_worker.exe") else original.read(item)
                )
        with self.assertRaisesRegex(P.PackageError, "hash mismatch"):
            P.verify_archive(altered)

    def test_archive_rejects_extra_traversal_and_case_collision(self):
        for entry in ("../escape", "host/extra.py", "HOST/bin/ch_worker.exe"):
            target = self.base / ("bad" + str(len(entry)) + ".zip")
            target.write_bytes(self.archive.read_bytes())
            with zipfile.ZipFile(target, "a") as archive:
                archive.writestr(entry, b"x")
            with self.subTest(entry=entry), self.assertRaises(P.PackageError):
                P.verify_archive(target)

    def test_requires_internal_opt_in(self):
        with self.assertRaisesRegex(P.PackageError, "allow-internal"):
            P.install(self.archive, self.root, self.prefs, self.data)
        self.assertFalse(self.prefs.exists())

    def test_install_relocates_and_uninstalls(self):
        result = self.install(expected_sha256=P.file_hash(self.archive))
        package = json.loads(Path(result["configuration"]).read_bytes())
        self.assertEqual("houdini_version == '22.0.429'", package["enable"])
        values = {key: value for item in package["env"] for key, value in item.items()}
        self.assertEqual(values["CYCLES_HOUDINI_SOURCE_ROOT"], self.data.as_posix())
        self.assertEqual(
            values["CYCLES_HOUDINI_HOST_ROOT"],
            (P.version_dir(self.root, self.manifest["package_id"]) / "host").as_posix(),
        )
        P.verify_installed(P.version_dir(self.root, self.manifest["package_id"]))
        P.uninstall(self.root)
        self.assertFalse(Path(result["configuration"]).exists())
        self.assertTrue(Path(result["host"]).is_dir())

    def test_preserves_previous_configuration_through_upgrade(self):
        path = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        path.parent.mkdir(parents=True)
        previous = b'{"enable": false, "custom": "preserved bytes"}\r\n'
        path.write_bytes(previous)
        with self.assertRaisesRegex(P.PackageError, "unmanaged"):
            self.install()
        self.install(replace_existing=True)
        fixture_archive(self.archive, b"updated native fixture")
        self.install()
        P.uninstall(self.root)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(len(list((self.root / "versions").iterdir())), 2)

    def test_refuses_external_config_edits_and_shared_root(self):
        result = self.install()
        Path(result["configuration"]).write_bytes(b'{"artist": "edited"}')
        with self.assertRaisesRegex(P.PackageError, "externally"):
            P.uninstall(self.root)
        with self.assertRaisesRegex(P.PackageError, "externally"):
            self.install()
        other = self.base / "shared"
        other.mkdir()
        (other / "unrelated.txt").write_text("keep")
        with self.assertRaisesRegex(P.PackageError, "not owned"):
            P.install(self.archive, other, self.prefs, self.data, allow_internal=True)

    def registration_status(self, **kwargs):
        return P.registration_status(self.root, self.prefs, self.data, "22.0.429", **kwargs)

    def test_registration_consent_survives_uninstall_and_reinstall(self):
        self.assertEqual(self.registration_status()["ownership"], "absent")
        self.assertFalse(self.root.exists(), "Read-only preflight created an installation root")
        path = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        path.parent.mkdir(parents=True)
        original = b'{"custom": "artist bytes"}\r\n'
        path.write_bytes(original)
        for roundtrip in range(2):
            with self.subTest(roundtrip=roundtrip):
                self.assertTrue(self.registration_status()["replace_existing_required"])
                output = io.StringIO()
                with redirect_stdout(output):
                    code = P.main(
                        [
                            "registration-status",
                            "--houdini",
                            "22.0.429",
                            "--install-root",
                            str(self.root),
                            "--preference-root",
                            str(self.prefs),
                            "--data-root",
                            str(self.data),
                        ]
                    )
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())["ownership"], "unmanaged")
                with self.assertRaisesRegex(P.PackageError, "unmanaged"):
                    self.install()
                self.assertEqual(path.read_bytes(), original)
                self.install(replace_existing=True)
                self.assertEqual(self.registration_status()["ownership"], "owned")
                self.install()  # Active repair does not request replacement consent.
                P.uninstall(self.root)
                self.assertEqual(path.read_bytes(), original)
                self.assertTrue((self.root / "state.json").exists())

    def test_registration_preflight_refuses_external_edits_and_foreign_ownership(self):
        self.install()
        other = self.base / "other"
        with self.assertRaisesRegex(P.PackageError, "owned by another"):
            P.registration_status(other, self.prefs, self.base / "other-data", "22.0.429")
        path = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        changed = b'{"artist": "edited active registration"}'
        path.write_bytes(changed)
        with self.assertRaisesRegex(P.PackageError, "externally"):
            self.registration_status()
        with self.assertRaisesRegex(P.PackageError, "externally"):
            self.install(replace_existing=True)
        self.assertEqual(path.read_bytes(), changed)

    def test_registration_preflight_refuses_pending_or_wrong_preferences(self):
        self.install(defer_commit=True, setup_token="unfinished")
        with self.assertRaisesRegex(P.PackageError, "recover"):
            self.registration_status()
        P.commit_setup(self.root, "unfinished")
        with self.assertRaisesRegex(P.PackageError, "preference root"):
            P.registration_status(self.root, self.base / "other-prefs", self.data, "22.0.429")

    def test_interrupted_activation_rolls_back_before_next_install(self):
        original = P.atomic_json

        def fail_state(path, value):
            if Path(path).name == "state.json":
                raise OSError("simulated power loss after package activation")
            original(path, value)

        with patch.object(P, "atomic_json", side_effect=fail_state), self.assertRaises(OSError):
            self.install()
        self.assertTrue((self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]).exists())
        with self.assertRaisesRegex(P.PackageError, "recover"):
            self.install()
        result = P.recover(self.root)
        self.assertEqual(len(result["recovered"]), 1)
        self.assertFalse((self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]).exists())
        self.install()

    def test_recovery_does_not_overwrite_subsequent_external_change(self):
        original = P.atomic_json

        def fail_state(path, value):
            if Path(path).name == "state.json":
                raise OSError("simulated interruption")
            original(path, value)

        with patch.object(P, "atomic_json", side_effect=fail_state), self.assertRaises(OSError):
            self.install()
        (self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]).write_bytes(b"external change")
        with self.assertRaisesRegex(P.PackageError, "changed after interruption"):
            P.recover(self.root)

    def test_extracted_payload_is_checked_and_reused_version_rejects_tampering(self):
        self.install()
        target = P.version_dir(self.root, self.manifest["package_id"]) / "host/bin/ch_worker.exe"
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(P.PackageError, "hash mismatch"):
            self.install()

    def test_install_lock_is_exclusive(self):
        root = P._owned_root(self.root, create=True)
        with P.installation_lock(root), self.assertRaisesRegex(P.PackageError, "installation lock"):
            with P.installation_lock(root):
                self.fail("acquired lock twice")

    def test_rejects_linked_preference_ancestor(self):
        actual = self.base / "actual"
        actual.mkdir()
        link = self.base / "linked"
        try:
            link.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires Windows developer mode or privilege")
        with self.assertRaisesRegex(P.PackageError, "Reparse/symlink"):
            P.install(self.archive, self.root, link / "nested", self.data, allow_internal=True)

    def test_public_archive_requires_recorded_gates(self):
        manifest = fixture_archive(self.archive, purpose="public")
        self.assertEqual(P.verify_archive(self.archive)["package_id"], manifest["package_id"])
        with zipfile.ZipFile(self.archive) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        manifest["audit"]["notices"] = "blocked"
        identity = dict(manifest)
        identity.pop("package_id")
        manifest["package_id"] = P.digest(P.canonical(identity))
        entries[P.MANIFEST] = P.canonical(manifest)
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(P.PackageError, "gates"):
            P.verify_archive(self.archive)

    def test_deterministic_build_and_relative_delegate(self):
        source = self.base / "source"
        source.mkdir()
        plugin = source / "plugInfo.json"
        plugin.write_text(json.dumps({"Plugins": [{"Info": {"Types": {"HdCyclesPlugin": {}}}}]}))
        binary = source / "worker"
        binary.write_bytes(b"fixture")
        plan = dict(
            files={"host/dso/usd_plugins/hdCycles/resources/plugInfo.json": plugin, "host/bin/ch_worker.exe": binary},
            runtime_fingerprint="b" * 64,
            manifest={"host_sdk": {"houdini": "22.0.429"}},
        )
        plan["runtime_overrides"] = P.runtime_overrides(plan["files"])
        report = dict(result="passed", gates={"build_sources": "passed", "dependencies": "passed", "notices": "passed"})
        first, second = self.base / "one.zip", self.base / "two.zip"
        with patch.object(P, "payload_plan", return_value=plan), patch.object(P, "audit", return_value=(report, {})):
            P.make_archive("unused", "unused", first)
            P.make_archive("unused", "unused", second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with self.assertRaisesRegex(P.PackageError, "certification"):
                P.make_archive("unused", "unused", self.base / "public.zip", purpose="public")
        with zipfile.ZipFile(first) as archive:
            plugin = json.loads(archive.read("host/dso/usd_plugins/hdCycles/resources/plugInfo.json"))["Plugins"][0]
            self.assertEqual((plugin["Root"], plugin["LibraryPath"]), ("..", "hdCycles.dll"))
        P.verify_archive(first)

    def test_deep_notice_paths_are_stored_flat_with_an_index(self):
        # A deep licence path of a component (hwloc's Android sample) is stored flat in the component's folder; the
        # short ones keep their names; the stored name depends only on the original path.
        deep = "contrib/android/AndroidApp/lstopo/src/main/java/com/hwloc/lstopo/ZoomView/licence.txt"
        flat = P.notice_destination("hwloc", deep)
        self.assertEqual(flat, P.notice_destination("hwloc", deep))
        self.assertRegex(flat, r"^notices/hwloc/[0-9a-f]{12}-licence\.txt$")
        self.assertLessEqual(len(flat), P.NOTICE_PATH_LIMIT)
        self.assertEqual(P.notice_destination("cycles", "LICENSE"), "notices/cycles/LICENSE")
        very_long = "a/" * 30 + "x" * 80 + ".txt"
        self.assertRegex(P.notice_destination("houdini-sdk-openimagedenoise", very_long), r"/[0-9a-f]{12}\.txt$")
        self.assertNotEqual(P.notice_destination("hwloc", deep), P.notice_destination("hwloc", deep.upper()))

    def test_every_catalogued_notice_fits_and_keeps_a_distinct_stored_name(self):
        # The real catalogue (packaging/notices/index.json, written by scripts/fetch-release-notices.py): every notice
        # text gets a stored name within the limit, no two share one, and only the deep ones are moved.
        catalog = json.loads((Path(__file__).parents[1] / "packaging/notices/index.json").read_text(encoding="utf-8"))
        stored = {}
        for component, record in catalog["components"].items():
            for notice in record.get("files", []):
                target = notice.get("target", Path(notice["path"]).name)
                name = P.notice_destination(component, target)
                self.assertLessEqual(len(name), P.NOTICE_PATH_LIMIT)
                self.assertNotIn(name.casefold(), stored)
                stored[name.casefold()] = (component, target)
                if len("notices/" + component + "/" + target) <= P.NOTICE_PATH_LIMIT:
                    self.assertEqual(name, "notices/" + component + "/" + target)
        self.assertGreater(len(stored), 50)

    def test_package_build_refuses_a_path_over_the_budget(self):
        self.assertEqual((P.BELOW_APP_BUDGET, P.PAYLOAD_PREFIX_CHARS, P.PACKAGE_PATH_BUDGET), (125, 39, 86))
        fits = "host/" + "a" * (P.PACKAGE_PATH_BUDGET - 5)
        self.assertIsNone(P.package_path_problem([fits]))
        too_long = "host/" + "a" * (P.PACKAGE_PATH_BUDGET - 4)
        self.assertIn("give that file a shorter name", P.package_path_problem([fits, too_long]))
        # The bytecode Houdini's Python may write later is not counted (Python skips it when it does not fit).
        self.assertIsNone(P.package_path_problem(["host/python3.13libs/" + "m" * 63 + ".py"]))
        source = self.base / "made-up-source"
        source.mkdir()
        (source / "f").write_bytes(b"made up")
        plan = dict(files={too_long: source / "f"}, runtime_fingerprint="b" * 64, manifest={"host_sdk": {"houdini": "22.0.429"}})
        plan["runtime_overrides"] = {}
        report = dict(result="passed", gates={"build_sources": "passed", "dependencies": "passed", "notices": "passed"})
        with patch.object(P, "payload_plan", return_value=plan), patch.object(P, "audit", return_value=(report, {})):
            with self.assertRaisesRegex(P.PackageError, "characters; at most 86 fit"):
                P.make_archive("unused", "unused", self.base / "long.zip")
        self.assertFalse((self.base / "long.zip").exists())

    def long_root(self, total):
        """Use a payload member long enough to exceed the journal file path."""
        self.manifest = fixture_archive(self.archive, extra_path="host/resources/" + "a" * 65 + ".txt")
        names = [entry["path"] for entry in self.manifest["files"]] + [P.MANIFEST]
        longest = max(len(n) for n in names)
        fixed = len(str(self.base)) + 1 + len("installation") + 1 + (P.PAYLOAD_PREFIX_CHARS - len("installation/")) + longest
        pad = total - fixed - 1
        if pad < 1:
            self.skipTest("the test folder is already too long for this case")
        return self.base / ("r" * pad) / "installation"

    def test_install_refuses_an_app_folder_that_is_too_long_before_writing(self):
        root = self.long_root(P.WINDOWS_PATH_LIMIT + 1)
        with self.assertRaisesRegex(P.PackageError, r"is too long for Cycles for Houdini: .* 260 characters and Windows allows 259, so choose a folder at least 1 character shorter"):
            P.install(self.archive, root, self.prefs, self.data, allow_internal=True)
        self.assertFalse(root.parent.exists())
        self.assertFalse(self.prefs.exists())
        self.assertFalse(self.data.exists())

    def test_install_just_under_the_limit_installs_recovers_and_uninstalls(self):
        root = self.long_root(P.WINDOWS_PATH_LIMIT)
        self.root = root
        original = P.atomic_json

        def fail_state(path, value):
            if Path(path).name == "state.json":
                raise OSError("simulated interruption")
            original(path, value)

        with patch.object(P, "atomic_json", side_effect=fail_state), self.assertRaises(OSError):
            self.install()
        self.assertEqual(len(P.recover(root)["recovered"]), 1)
        self.install()
        version = P.version_dir(root, self.manifest["package_id"])
        self.assertEqual(version.name, self.manifest["package_id"][:16])
        longest = max(len(str(p)) for p in version.rglob("*"))
        self.assertEqual(longest, P.WINDOWS_PATH_LIMIT)
        P.uninstall(root)

    def test_preference_budget_includes_atomic_file_and_counts_utf16_units(self):
        filename = P.target_identity("22.0.429")["package_filename"]
        name = Path("packages") / filename
        suffix = P.atomic_write_path(name, "0" * 32)
        width = P.WINDOWS_PATH_LIMIT - P.windows_path_length(self.base / suffix) - 1
        if width < 2:
            self.skipTest("the test folder is already too long for this case")
        # The emoji consumes two UTF-16 code units in a Windows file path.
        fits = self.base / ("f" * (width - 2) + "\U0001f333")
        too_long = fits.with_name(fits.name + "f")
        self.assertEqual(P.windows_path_length(fits / suffix), P.WINDOWS_PATH_LIMIT)
        self.assertEqual(P.windows_path_length(too_long / suffix), P.WINDOWS_PATH_LIMIT + 1)
        with self.assertRaisesRegex(P.PackageError, r"a path of 260 characters"):
            P.registration_status(self.root, too_long, self.data, "22.0.429", archive=self.archive)
        with self.assertRaisesRegex(P.PackageError, r"a path of 260 characters"):
            P.install(self.archive, self.root, too_long, self.data, allow_internal=True)
        self.assertFalse(too_long.exists())
        self.assertFalse(self.root.exists())
        app = self.base / "app"
        install_root = app / "installation"
        P.install(self.archive, install_root, fits, self.data, allow_internal=True, application_root=app)
        self.assertTrue((fits / name).exists())
        if os.name == "nt":
            result = self.windows_maintenance("Uninstall", app)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            P.uninstall(install_root)
        self.assertFalse((fits / name).exists())

    def test_install_path_preflight_checks_transaction_helpers_for_short_payload(self):
        folder = self.base / ("r" * (P.WINDOWS_PATH_LIMIT - P.windows_path_length(self.base) - 35))
        self.assertIn("is too long", P.install_path_problem(folder / "installation", ["a"], folder))

    def test_a_payload_installed_with_the_long_folder_name_is_still_found(self):
        self.install()
        short = P.version_dir(self.root, self.manifest["package_id"])
        legacy = short.parent / self.manifest["package_id"]
        short.rename(legacy)
        self.assertEqual(P.version_dir(self.root, self.manifest["package_id"]), legacy)
        P.verify_installed(legacy)
        P.uninstall(self.root)

    def legacy_install(self, original=b'{"enable":false,"unmanaged":"original bytes"}\r\n'):
        self.install()
        state = P.read_json(self.root / "state.json")
        exact = Path(state["package_path"])
        generic = self.prefs / "packages" / P.PACKAGE_FILE
        exact.replace(generic)
        for name in ("houdini", "package_filename", "app_id"):
            state.pop(name)
        state.update(package_path=str(generic), original_package=P._encoded(original))
        P.atomic_json(self.root / "state.json", state)
        marker = P.read_json(self.root / P.INSTALL_MARKER)
        for name in P.target_identity("22.0.429"):
            marker.pop(name, None)
        P.atomic_json(self.root / P.INSTALL_MARKER, marker)
        return generic, original

    def test_target_identity_stable_and_qualified(self):
        first, second = (P.target_identity(host) for host in P.QUALIFIED_HOSTS)
        self.assertNotEqual(first["app_id"], second["app_id"])
        self.assertEqual(first, P.target_identity("22.0.429"))
        self.assertEqual(second["package_filename"], "cycles_houdini_22_0_466.json")
        with self.assertRaisesRegex(P.PackageError, "Unqualified"):
            P.target_identity("22.0.999")

    def test_install_collision_uses_exact_eligible_version_intersection(self):
        sdk_root = self.base / "Program Files/Side Effects Software"
        for version, api in (("22.0.429", 22000425), ("22.0.466", 22000453), ("22.0.467", 22000453)):
            include = sdk_root / ("Houdini " + version) / "toolkit/include"
            (include / "SYS").mkdir(parents=True)
            (include / "UT").mkdir()
            (include / "SYS/SYS_Version.h").write_text(f'#define SYS_VERSION_FULL "{version}"\n')
            (include / "UT/UT_HDKVersion.h").write_text(f"#define HDK_API_VERSION {api}\n")
        packages = self.prefs / "packages"
        packages.mkdir(parents=True)
        other = packages / "cycles_houdini_other.json"
        with patch.dict(os.environ, ProgramFiles=str(self.base / "Program Files")):
            P.atomic_json(other, dict(enable="houdini_version == '22.0.429'"))
            P.validate_install_paths(self.root, self.prefs, self.data, "22.0.466")
            P.atomic_json(other, dict(enable="houdini_version == '22.0.467'"))
            with self.assertRaisesRegex(P.PackageError, "same Houdini build"):
                P.validate_install_paths(self.root, self.prefs, self.data, "22.0.466")
            P.atomic_json(other, dict(enable="houdini_version >= '22.0'"))
            with self.assertRaisesRegex(P.PackageError, "Unparseable"):
                P.validate_install_paths(self.root, self.prefs, self.data, "22.0.466")

    def test_setup_preflight_uses_archive_oidn_identity_for_exact_collision_set(self):
        sdk_root = self.base / "Program Files/Side Effects Software"
        for version, runtime in (("22.0.466", b"matching"), ("22.0.467", b"different")):
            root = sdk_root / ("Houdini " + version)
            include = root / "toolkit/include"
            (include / "SYS").mkdir(parents=True)
            (include / "UT").mkdir()
            (include / "SYS/SYS_Version.h").write_text(f'#define SYS_VERSION_FULL "{version}"\n')
            (include / "UT/UT_HDKVersion.h").write_text("#define HDK_API_VERSION 22000453\n")
            (root / "bin").mkdir()
            (root / "bin/OpenImageDenoise.dll").write_bytes(runtime)
        provider = dict(
            hdk_api=22000453,
            files=[dict(path="bin/OpenImageDenoise.dll", kind="runtime", bytes=8, sha256=P.digest(b"matching"))],
        )
        fixture_archive(self.archive, houdini="22.0.466", provider=provider)
        self.prefs.joinpath("packages").mkdir(parents=True)
        other = self.prefs / "packages/cycles_houdini_other.json"
        P.atomic_json(other, dict(enable="houdini_version == '22.0.467'"))
        with patch.dict(os.environ, ProgramFiles=str(self.base / "Program Files")):
            self.assertEqual(
                P.registration_status(self.root, self.prefs, self.data, "22.0.466", archive=self.archive)["ownership"],
                "absent",
            )
            P.install(self.archive, self.root, self.prefs, self.data, allow_internal=True)
            (sdk_root / "Houdini 22.0.467/bin/OpenImageDenoise.dll").write_bytes(b"matching")
            with self.assertRaisesRegex(P.PackageError, "same Houdini build"):
                P.registration_status(self.root, self.prefs, self.data, "22.0.466", archive=self.archive)

    def test_shared_preferences_targets_upgrade_uninstall_independently(self):
        self.install()
        first = self.prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        before = first.read_bytes()
        sibling, data = self.base / "sibling", self.base / "sibling-data"
        fixture_archive(self.archive, houdini="22.0.466")
        result = P.install(self.archive, sibling, self.prefs, data, allow_internal=True)
        second = Path(result["configuration"])
        sibling_before = second.read_bytes()
        fixture_archive(self.archive, payload=b"upgrade429")
        self.install()
        self.assertEqual(second.read_bytes(), sibling_before)
        self.assertNotEqual(first.read_bytes(), before)
        P.uninstall(self.root)
        self.assertEqual(second.read_bytes(), sibling_before)
        P.verify_installed(P.version_dir(sibling, P.read_json(sibling / "state.json")["active"]))
        P.uninstall(sibling)
        self.assertFalse(second.exists())

    def test_target_root_reuse_and_overlap_rejected(self):
        self.install()
        fixture_archive(self.archive, houdini="22.0.466")
        with self.assertRaisesRegex(P.PackageError, "another Houdini target"):
            P.install(self.archive, self.root, self.prefs, self.data, allow_internal=True)
        with self.assertRaisesRegex(P.PackageError, "overlap"):
            P.install(self.archive, self.root / "nested", self.prefs, self.base / "data2", allow_internal=True)
        with self.assertRaisesRegex(P.PackageError, "overlap"):
            P.install(self.archive, self.base / "new", self.prefs, self.prefs / "data", allow_internal=True)

    def test_legacy_sibling_is_preserved_and_matching_target_migrates(self):
        generic, original = self.legacy_install()
        previous = generic.read_bytes()
        old_state = (self.root / "state.json").read_bytes()
        sibling = self.base / "sibling466"
        fixture_archive(self.archive, houdini="22.0.466")
        P.install(self.archive, sibling, self.prefs, self.base / "data466", allow_internal=True)
        self.assertEqual(generic.read_bytes(), previous)
        self.assertEqual((self.root / "state.json").read_bytes(), old_state)
        upgraded = self.base / "new429"
        fixture_archive(self.archive, payload=b"updated429")
        P.install(self.archive, upgraded, self.prefs, self.base / "data429", allow_internal=True)
        self.assertFalse(generic.exists())
        self.assertIsNone(P.read_json(self.root / "state.json")["active"])
        with self.assertRaisesRegex(P.PackageError, "No active installation"):
            P.uninstall(self.root)
        P.uninstall(upgraded)
        self.assertEqual(generic.read_bytes(), original)
        self.assertTrue((self.prefs / "packages" / "cycles_houdini_22_0_466.json").exists())

    def test_interrupted_legacy_migration_restores_every_owned_file(self):
        generic, _ = self.legacy_install()
        before_package, before_state = generic.read_bytes(), (self.root / "state.json").read_bytes()
        upgraded = self.base / "new429"
        write = P.atomic_json

        def fail(path, value):
            if Path(path) == upgraded / "state.json":
                raise OSError("interrupted legacy handover")
            write(path, value)

        with patch.object(P, "atomic_json", side_effect=fail), self.assertRaises(OSError):
            P.install(self.archive, upgraded, self.prefs, self.base / "new-data", allow_internal=True)
        self.assertFalse(generic.exists())
        P.recover(upgraded)
        self.assertEqual(generic.read_bytes(), before_package)
        self.assertEqual((self.root / "state.json").read_bytes(), before_state)
        self.assertFalse((self.prefs / "packages" / "cycles_houdini_22_0_429.json").exists())

    def test_ambiguous_legacy_and_registry_delete_tree_preserved(self):
        generic = self.prefs / "packages" / P.PACKAGE_FILE
        generic.parent.mkdir(parents=True)
        generic.write_bytes(b'{"enable":true,"custom":"artist"}')
        with self.assertRaisesRegex(P.PackageError, "ambiguous"):
            self.install()
        self.assertEqual(generic.read_bytes(), b'{"enable":true,"custom":"artist"}')
        generic.unlink()
        old_app = self.base / "legacy-custom-app"
        with patch.object(P, "windows_installations", return_value=[dict(path=old_app, app_id=str(P.APP_NAMESPACE))]):
            for root in (old_app / "new-target" / "installation", old_app.parent):
                with self.assertRaisesRegex(P.PackageError, "overlap"):
                    P.install(self.archive, root, self.prefs, self.data, allow_internal=True)

    def test_setup_activation_is_pending_until_commit_or_recovers_on_failure(self):
        self.install(defer_commit=True)
        self.assertEqual(len(P._pending(self.root)), 1)
        P.recover(self.root)
        self.assertFalse((self.prefs / "packages" / "cycles_houdini_22_0_429.json").exists())
        self.install(defer_commit=True)
        P.commit_setup(self.root)
        self.assertFalse(P._pending(self.root))
        before = (self.prefs / "packages" / "cycles_houdini_22_0_429.json").read_bytes()
        fixture_archive(self.archive, payload=b"setup repair")
        self.install(defer_commit=True)
        P.recover(self.root)
        self.assertEqual((self.prefs / "packages" / "cycles_houdini_22_0_429.json").read_bytes(), before)

    def test_setup_retry_recovers_prior_unfinished_handover(self):
        generic, _ = self.legacy_install()
        upgraded = self.base / "new429"
        P.install(
            self.archive,
            upgraded,
            self.prefs,
            self.base / "new-data",
            allow_internal=True,
            defer_commit=True,
            setup_token="first",
        )
        self.assertFalse(generic.exists())
        with self.assertRaisesRegex(P.PackageError, "recover"):
            P.install(
                self.archive,
                upgraded,
                self.prefs,
                self.base / "new-data",
                allow_internal=True,
                defer_commit=True,
                setup_token="second",
            )
        self.assertEqual(P.abort_setup(upgraded, "second")["recovered"], [])
        P.abort_setup(upgraded, "first")
        P.install(self.archive, upgraded, self.prefs, self.base / "new-data", allow_internal=True, defer_commit=True)
        self.assertFalse(generic.exists())
        P.commit_setup(upgraded)
        self.assertFalse(P._pending(upgraded))

    def test_recovery_rejects_extra_change_outside_owned_paths(self):
        self.install(defer_commit=True)
        txpath = P._pending(self.root)[0]
        tx = P.read_json(txpath)
        artist = self.base / "artist.txt"
        artist.write_bytes(b"keep")
        tx["operation"] = "uninstall"
        tx["extra_changes"] = [dict(path=str(artist), previous=None, intended=P._encoded(b"delete me"))]
        P.atomic_json(txpath, tx)
        with self.assertRaisesRegex(P.PackageError, "escapes"):
            P.recover(self.root)
        self.assertEqual(artist.read_bytes(), b"keep")

    def test_same_target_windows_app_cannot_move_or_take_over_owned_registration(self):
        self.install()
        identity = P.target_identity("22.0.429")
        other = self.base / "other"
        with self.assertRaisesRegex(P.PackageError, "owned by another"):
            P.install(
                self.archive, other, self.prefs, self.base / "other-data", allow_internal=True, replace_existing=True
            )
        with patch.object(P, "windows_installations", return_value=[dict(path=self.root, app_id=identity["app_id"])]):
            with self.assertRaisesRegex(P.PackageError, "already installed"):
                P.install(self.archive, other, self.base / "other-prefs", self.base / "other-data", allow_internal=True)

    def test_isolated_release_alongside_same_target_windows_setup(self):
        app, prefs = self.base / "windows-app", self.base / "artist-prefs"
        P.install(self.archive, app / "installation", prefs, app / "data", allow_internal=True, application_root=app)
        registration = prefs / "packages" / P.target_identity("22.0.429")["package_filename"]
        original = registration.read_bytes()
        entry = dict(path=app, app_id=P.target_identity("22.0.429")["app_id"])
        with patch.object(P, "windows_installations", return_value=[entry]):
            self.install(isolated=True)
            self.assertTrue(P.uninstall(self.root)["result"] == "passed")
            for root, preferences, data in (
                (self.root, prefs, self.data),
                (app / "sandbox", self.prefs, self.data),
                (self.root, self.prefs, prefs / "data"),
            ):
                with (
                    self.subTest(root=root, preferences=preferences, data=data),
                    self.assertRaisesRegex(P.PackageError, "overlaps"),
                ):
                    P.install(self.archive, root, preferences, data, allow_internal=True, isolated=True)
            with self.assertRaisesRegex(P.PackageError, "Windows application"):
                self.install(application_root=self.root, isolated=True)
        self.assertEqual(registration.read_bytes(), original)

    def test_config_arrival_during_extraction_is_preserved(self):
        verify = P.verify_installed
        registration = self.prefs / "packages" / "cycles_houdini_22_0_429.json"

        def arrive(*args, **kwargs):
            value = verify(*args, **kwargs)
            registration.parent.mkdir(parents=True, exist_ok=True)
            registration.write_bytes(b"artist change during extraction")
            return value

        with (
            patch.object(P, "verify_installed", side_effect=arrive),
            self.assertRaisesRegex(P.PackageError, "changed during"),
        ):
            self.install()
        self.assertEqual(registration.read_bytes(), b"artist change during extraction")

    def test_transaction_cannot_be_transplanted_to_another_target_or_edited_state(self):
        self.install(defer_commit=True)
        original_tx = P._pending(self.root)[0]
        other = self.base / "other466"
        fixture_archive(self.archive, houdini="22.0.466")
        P.install(self.archive, other, self.prefs, self.base / "data466", allow_internal=True, defer_commit=True)
        other_tx = P._pending(other)[0]
        P.atomic_bytes(other_tx, original_tx.read_bytes())
        before = (self.prefs / "packages" / "cycles_houdini_22_0_429.json").read_bytes()
        with self.assertRaisesRegex(P.PackageError, "another installation"):
            P.recover(other)
        self.assertEqual((self.prefs / "packages" / "cycles_houdini_22_0_429.json").read_bytes(), before)
        state = P.read_json(self.root / "state.json")
        state["artist"] = "changed receipt"
        P.atomic_json(self.root / "state.json", state)
        with self.assertRaisesRegex(P.PackageError, "state changed"):
            P.recover(self.root)

    def test_setup_abort_before_first_state_and_recovery_journal_retry(self):
        original_write = P.atomic_json

        def fail_state(path, value):
            if Path(path) == self.root / "state.json":
                raise OSError("no initial state")
            original_write(path, value)

        with patch.object(P, "atomic_json", side_effect=fail_state), self.assertRaises(OSError):
            self.install(defer_commit=True, setup_token="setup-one")
        self.assertFalse((self.root / "state.json").exists())
        self.assertEqual(P.abort_setup(self.root, "setup-two")["recovered"], [])

        def fail_journal_commit(path, value):
            if value.get("state") == "rolled_back":
                raise OSError("rollback journal interrupted")
            original_write(path, value)

        with patch.object(P, "atomic_json", side_effect=fail_journal_commit), self.assertRaises(OSError):
            P.abort_setup(self.root, "setup-one")
        self.assertFalse((self.root / "state.json").exists())
        self.assertEqual(len(P.abort_setup(self.root, "setup-one")["recovered"]), 1)

    def test_disabled_legacy_owned_edit_conflicts_but_unmanaged_disabled_stays(self):
        generic, _ = self.legacy_install()
        value = P.read_json(generic)
        value["enable"] = False
        P.atomic_json(generic, value)
        edited = generic.read_bytes()
        old_state = (self.root / "state.json").read_bytes()
        with self.assertRaisesRegex(P.PackageError, "ownership is ambiguous"):
            P.install(self.archive, self.base / "new429", self.prefs, self.base / "new-data", allow_internal=True)
        self.assertEqual(generic.read_bytes(), edited)
        self.assertEqual((self.root / "state.json").read_bytes(), old_state)
        # A user's deliberately disabled generic file has no installer owner.
        value["env"] = [{"CYCLES_HOUDINI_HOST_ROOT": str(self.base / "unmanaged" / "host")}]
        P.atomic_json(generic, value)
        unmanaged = generic.read_bytes()
        P.install(self.archive, self.base / "new429", self.prefs, self.base / "new-data", allow_internal=True)
        self.assertEqual(generic.read_bytes(), unmanaged)
        P.uninstall(self.base / "new429")
        self.assertEqual(generic.read_bytes(), unmanaged)

    def payload_fixture(self, with_descriptor=False, public_root=None):
        build, source = self.base / "build", self.base / "source"
        host = build / "hosts/fixture"
        for directory in (host / "resources", source):
            directory.mkdir(parents=True)
        for component in P.RUNTIME_COMPONENTS:
            (source / "third_party/cycles/lib/windows_x64" / component / "bin").mkdir(parents=True)
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            (source / name).write_text("fixture")
        for name in (
            "scripts/maintain-registration.ps1",
            "scripts/refresh-registration.ps1",
            "launchers/Refresh Cycles registration.cmd",
        ):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        provider = dict(provider="houdini-sdk", version="2.3.3", houdini="22.0.429", redistributed=False, files=[])
        P.atomic_json(
            source / "source.lock.json",
            dict(
                sdk_dependencies=dict(openimagedenoise=provider),
                cycles=dict(commit="fixture-base", patches=[], patch_sha256={}),
            ),
        )
        (source / "patches").mkdir()
        P.atomic_json(
            source / "patches/triage.json",
            dict(upstream="fixture-base", patches=[], approved_features={}, upstream_queue={}, reviewed_exceptions={}),
        )
        (source / "docs/decisions").mkdir(parents=True)
        (source / "docs/decisions/0005-cycles-fork-policy.md").write_text("Fixture policy")
        paths = [
            "bin/ch_worker.exe",
            "dso/_cycles_houdini.dll",
            "resources/sdk-dependencies.json",
            "python3.13libs/cycles_houdini/viewer.py",
        ]
        paths.extend("config/branding/" + name for name in P.REQUIRED_BRANDING)
        paths.extend("blender/" + name for name in P.BLENDER_COMPONENT)
        if with_descriptor:
            paths.append("dso/usd_plugins/hdCycles/hdCycles.dll")
            paths.append(P.HYDRA_DESCRIPTOR.removeprefix("host/"))
        for name in paths:
            path = host / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original fixture")
        P.atomic_json(host / "resources/sdk-dependencies.json", dict(openimagedenoise=provider))
        if with_descriptor:
            P.atomic_json(
                host / paths[-1],
                dict(
                    Plugins=[
                        dict(
                            Name="hdCycles",
                            Type="library",
                            Root="E:/developer/build-a",
                            LibraryPath="E:/developer/build-a/hdCycles.dll",
                            ResourcePath="E:/developer/build-a/resources",
                            Info=dict(
                                Types=dict(
                                    HdCyclesPlugin=dict(
                                        displayName="Cycles (Development)", bases=["HdRendererPlugin"], priority=0
                                    )
                                )
                            ),
                        )
                    ]
                ),
            )
        manifest = dict(
            resource_root="hosts/fixture/resources",
            host_runtime=[],
            binary_artifacts=[],
            kernels=[],
            integration_source=[],
            applied_patches=[],
            features={},
            actual_revisions=dict(cycles="fixture-base"),
            host_sdk=dict(houdini="22.0.429"),
            houdini_root=str(self.base / "unused-houdini"),
            dependency_providers=dict(openimagedenoise=provider),
        )
        manifest["features"]["WITH_CYCLES_HYDRA_RENDER_DELEGATE"] = "ON" if with_descriptor else "OFF"
        if public_root:
            seal = P.ASSET_SEAL.removeprefix("host/")
            paths.append(seal)
            P.atomic_json(host / seal, dict(recipe=dict(houdini_root=public_root)))
            manifest.update(
                houdini_root=public_root,
                compiler=dict(path=r"C:\Users\fixture\compiler.exe", version="fixture"),
                ocio=dict(config=r"E:\private\ocio.ocio", sha256="0" * 64),
                build_host=dict(gpus=[dict(uuid="GPU-private-id", pci_bus_id="private-pci-id", name="Fixture GPU")]),
            )

        def save():
            manifest["host_runtime"] = [
                dict(path=path.relative_to(build).as_posix(), sha256=P.file_hash(path))
                for path in (host / name for name in paths)
            ]
            P.atomic_json(build / "build-manifest.json", manifest)

        save()
        return build, source, host, provider, save

    def test_packaged_fork_policy_matches_built_identity(self):
        build, source, host, provider, save = self.payload_fixture()
        manifest = P.read_json(build / "build-manifest.json")
        files = P.fork_policy_files(source, manifest)
        self.assertIn("patches/triage.json", files)
        self.assertIn("docs/decisions/0005-cycles-fork-policy.md", files)
        manifest["actual_revisions"]["cycles"] = "unreviewed-engine"
        with self.assertRaisesRegex(P.PackageError, "Fork policy differs"):
            P.fork_policy_files(source, manifest)
        manifest["actual_revisions"]["cycles"] = "fixture-base"
        manifest["applied_patches"] = [dict(path="unreviewed.patch", sha256="0" * 64)]
        with self.assertRaisesRegex(P.PackageError, "Fork policy differs"):
            P.fork_policy_files(source, manifest)

    def test_payload_rejects_branding_omitted_from_frozen_inventory(self):
        build, source, host, provider, _ = self.payload_fixture()
        manifest = P.read_json(build / "build-manifest.json")
        for name in P.REQUIRED_BRANDING:
            relative = "config/branding/" + name
            with self.subTest(name=name):
                self.assertTrue((host / relative).is_file())
                broken = dict(
                    manifest,
                    host_runtime=[r for r in manifest["host_runtime"] if not r["path"].endswith("/" + relative)],
                )
                P.atomic_json(build / "build-manifest.json", broken)
                with (
                    patch.object(P, "sdk_dependency_record", return_value=provider),
                    self.assertRaisesRegex(P.PackageError, "Required branding missing"),
                ):
                    P.payload_plan(build, source)

    def test_payload_rejects_blender_component_omitted_from_frozen_inventory(self):
        build, source, host, provider, _ = self.payload_fixture()
        manifest = P.read_json(build / "build-manifest.json")
        for name in P.BLENDER_COMPONENT:
            relative = "blender/" + name
            with self.subTest(name=name):
                self.assertTrue((host / relative).is_file())
                broken = dict(
                    manifest,
                    host_runtime=[r for r in manifest["host_runtime"] if not r["path"].endswith("/" + relative)],
                )
                P.atomic_json(build / "build-manifest.json", broken)
                with (
                    patch.object(P, "sdk_dependency_record", return_value=provider),
                    self.assertRaisesRegex(P.PackageError, "Blender component missing"),
                ):
                    P.payload_plan(build, source)

    def test_optix_notices_follow_the_built_backend_and_kernel_payload(self):
        build, source, host, provider, _ = self.payload_fixture()
        path = build / "build-manifest.json"
        manifest = P.read_json(path)
        kernel = host / "resources/lib/kernel_optix.ptx.zst"
        kernel.parent.mkdir()
        kernel.write_bytes(b"optix fixture")
        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
        ):
            for enabled, shipped in [(False, False), (True, False), (False, True)]:
                manifest["features"]["WITH_CYCLES_DEVICE_OPTIX"] = "ON" if enabled else "OFF"
                manifest["kernels"] = [dict(name=kernel.name, sha256=P.file_hash(kernel))] if shipped else []
                P.atomic_json(path, manifest)
                self.assertEqual("optix-headers" in P.payload_plan(build, source)["components"], enabled or shipped)

    def test_optix_header_notice_is_mandatory_even_for_internal_packages(self):
        build, source, host, provider, _ = self.payload_fixture()
        repository = Path(__file__).resolve().parents[1]
        notices = Path("packaging/notices")
        shutil.copytree(repository / notices / "optix-headers", source / notices / "optix-headers")
        record = P.read_json(repository / notices / "index.json")["components"]["optix-headers"]
        P.atomic_json(source / notices / "index.json", dict(components={"optix-headers": record}))
        manifest = P.read_json(build / "build-manifest.json")
        manifest["features"]["WITH_CYCLES_DEVICE_OPTIX"] = "ON"
        manifest["actual_revisions"]["optix"] = record["version"]
        P.atomic_json(build / "build-manifest.json", manifest)
        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
        ):
            plan = P.payload_plan(build, source)
            report, files = P.audit(plan)
            self.assertEqual({Path(p).name for p in files}, set(P.OPTIX_NOTICE_FILES))
            for name in P.OPTIX_NOTICE_FILES:
                path = source / notices / "optix-headers" / name
                original = path.read_bytes()
                for damaged in (False, True):
                    if damaged:
                        path.write_bytes(b"changed")
                    else:
                        path.unlink()
                    with self.subTest(name=name, damaged=damaged):
                        report, files = P.audit(plan)
                        entry = next(item for item in report["licenses"] if item["component"] == "optix-headers")
                        self.assertIn("Required OptiX notice unavailable: " + name, entry["reasons"])
                        with self.assertRaisesRegex(P.PackageError, "verified SDK and header notices"):
                            P.make_archive(build, source, self.base / "missing-notice.zip")
                    path.write_bytes(original)
            record["files"] = [f for f in record["files"] if f["target"] != "HEADER-NOTICES.txt"]
            P.atomic_json(source / notices / "index.json", dict(components={"optix-headers": record}))
            with self.assertRaisesRegex(P.PackageError, "verified SDK and header notices"):
                P.make_archive(build, source, self.base / "incomplete-catalog.zip")

    def test_openpgl_enabled_and_linked_builds_require_pinned_complete_notices(self):
        build, source, host, provider, _ = self.payload_fixture()
        repository = Path(__file__).resolve().parents[1]
        notices = Path("packaging/notices")
        shutil.copytree(repository / notices / "openpgl", source / notices / "openpgl")
        record = P.read_json(repository / notices / "index.json")["components"]["openpgl"]
        catalog = source / notices / "index.json"
        manifest_path = build / "build-manifest.json"
        manifest = P.read_json(manifest_path)
        manifest["actual_revisions"]["dependencies"] = record["provenance"]["revision"]
        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
        ):
            for enabled, linked in [(False, False), (True, False), (False, True)]:
                manifest["features"]["WITH_CYCLES_PATH_GUIDING"] = "ON" if enabled else "OFF"
                P.atomic_json(manifest_path, manifest)
                (build / "build.ninja").write_text(
                    "  LINK_LIBRARIES = third_party/cycles/lib/windows_x64/openpgl/lib/openpgl.lib\n" if linked else ""
                )
                with self.subTest(enabled=enabled, linked=linked):
                    self.assertEqual("openpgl" in P.payload_plan(build, source)["components"], enabled or linked)
            plan = P.payload_plan(build, source)
            report, _ = P.audit(plan)
            entry = next(item for item in report["licenses"] if item["component"] == "openpgl")
            self.assertIn("No pinned notice inventory", entry["reasons"])
            with self.assertRaisesRegex(P.PackageError, "verified OpenPGL dependency notices"):
                P.make_archive(build, source, self.base / "missing-openpgl-catalog.zip")
            P.atomic_json(catalog, dict(components=dict(openpgl=record)))
            report, files = P.audit(plan)
            entry = next(item for item in report["licenses"] if item["component"] == "openpgl")
            self.assertEqual(entry["result"], "passed")
            self.assertEqual({Path(p).name for p in files}, set(P.OPENPGL_NOTICE_FILES))
            for name in P.OPENPGL_NOTICE_FILES:
                path = source / notices / "openpgl" / name
                original = path.read_bytes()
                for damaged in (False, True):
                    if damaged:
                        path.write_bytes(b"changed")
                    else:
                        path.unlink()
                    with self.subTest(name=name, damaged=damaged):
                        report, _ = P.audit(plan)
                        entry = next(item for item in report["licenses"] if item["component"] == "openpgl")
                        self.assertEqual(entry["result"], "blocked")
                        self.assertIn("Required OpenPGL notice unavailable: " + name, entry["reasons"])
                        with self.assertRaisesRegex(P.PackageError, "verified OpenPGL dependency notices"):
                            P.make_archive(build, source, self.base / "missing-openpgl-notice.zip")
                    path.write_bytes(original)
            record["provenance"]["revision"] = "wrong-dependency-revision"
            P.atomic_json(catalog, dict(components=dict(openpgl=record)))
            report, _ = P.audit(plan)
            entry = next(item for item in report["licenses"] if item["component"] == "openpgl")
            self.assertIn("OpenPGL notices do not match the dependency revision in the build", entry["reasons"])
            with self.assertRaisesRegex(P.PackageError, "verified OpenPGL dependency notices"):
                P.make_archive(build, source, self.base / "wrong-openpgl-revision.zip")
            record["provenance"]["revision"] = manifest["actual_revisions"]["dependencies"]
            record["files"] = [item for item in record["files"] if item["target"] != "third-party-programs-ISPC.txt"]
            P.atomic_json(catalog, dict(components=dict(openpgl=record)))
            report, _ = P.audit(plan)
            entry = next(item for item in report["licenses"] if item["component"] == "openpgl")
            self.assertIn("Required OpenPGL notice unavailable: third-party-programs-ISPC.txt", entry["reasons"])
            with self.assertRaisesRegex(P.PackageError, "verified OpenPGL dependency notices"):
                P.make_archive(build, source, self.base / "incomplete-openpgl-catalog.zip")

    def test_python_runtime_edits_invalidate_certification_fingerprint(self):
        build, source, host, provider, save = self.payload_fixture()
        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
        ):
            before = P.payload_plan(build, source)["runtime_fingerprint"]
            (host / "python3.13libs/cycles_houdini/viewer.py").write_bytes(
                b"changed only Python UI/capture implementation"
            )
            save()
            after = P.payload_plan(build, source)["runtime_fingerprint"]
        self.assertNotEqual(before, after)

    def test_transformed_runtime_bytes_invalidate_stale_public_certificate(self):
        build, source, host, provider, _ = self.payload_fixture(
            with_descriptor=True, public_root=r"C:\Program Files\Side Effects Software\Houdini 22.0.429"
        )
        descriptor = host / P.HYDRA_DESCRIPTOR.removeprefix("host/")
        original_descriptor = descriptor.read_bytes()
        original_transform = P.runtime_overrides

        def changed_transform(files):
            replacements = original_transform(files)
            value = json.loads(replacements[P.HYDRA_DESCRIPTOR])
            value["Plugins"][0]["ResourcePath"] = "../../../alternate_resources"
            replacements[P.HYDRA_DESCRIPTOR] = P.canonical(value)
            return replacements

        def passing_audit(plan):
            return dict(
                result="passed",
                runtime_fingerprint=plan["runtime_fingerprint"],
                gates=dict(build_sources="passed", dependencies="passed", notices="passed"),
            ), {}

        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
            patch.object(P, "audit", side_effect=passing_audit),
        ):
            before = P.payload_plan(build, source)
            report, notices = P.audit(before)
            files, manifest = P.archive_contents(before, report, notices, "public")
            certificate = self.base / "certificate.json"
            P.atomic_json(certificate, P.public_certificate_fields(manifest, files[P.BUILD_PROVENANCE]))
            accepted = self.base / "accepted-public.zip"
            P.make_archive(build, source, accepted, purpose="public", certification=certificate)
            self.assertEqual(P.verify_archive(accepted)["runtime_fingerprint"], before["runtime_fingerprint"])
            with patch.object(P, "runtime_overrides", side_effect=changed_transform):
                after = P.payload_plan(build, source)
                self.assertNotEqual(before["runtime_fingerprint"], after["runtime_fingerprint"])
                self.assertEqual(descriptor.read_bytes(), original_descriptor)
                rejected = self.base / "rejected-public.zip"
                with self.assertRaisesRegex(P.PackageError, "certification"):
                    P.make_archive(build, source, rejected, purpose="public", certification=certificate)
                self.assertFalse(rejected.exists())
                emitted = self.base / "transformed-internal.zip"
                P.make_archive(build, source, emitted)
                self.assertEqual(P.verify_archive(emitted)["runtime_fingerprint"], after["runtime_fingerprint"])
                with zipfile.ZipFile(emitted) as archive:
                    actual = archive.read(P.HYDRA_DESCRIPTOR)
                self.assertEqual(actual, after["runtime_overrides"][P.HYDRA_DESCRIPTOR])
                self.assertEqual(json.loads(actual)["Plugins"][0]["ResourcePath"], "../../../alternate_resources")

    def test_portable_public_provenance_binds_payload_and_parent(self):
        sdk_root = r"D:\SDK\Houdini 22.0.429"
        build, source, host, provider, _ = self.payload_fixture(public_root=sdk_root)
        original = (build / "build-manifest.json").read_bytes()
        certificate_path = self.base / "certificate.json"
        kwargs = dict(purpose="public", certification=certificate_path, public_sdk_roots=(sdk_root,))
        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
        ):
            plan = P.payload_plan(build, source)
            self.assertTrue(plan["components"])
            with self.assertRaisesRegex(P.PackageError, "complete pinned notice"):
                P.make_archive(build, source, self.base / "blocked.zip", **kwargs)
            catalog = source / "packaging/notices/index.json"
            catalog.parent.mkdir(parents=True)
            notice = catalog.parent / "FIXTURE-LICENSE.txt"
            notice.write_text("Synthetic redistribution license fixture.")
            P.atomic_json(
                catalog,
                dict(
                    components={
                        name: dict(
                            version="fixture",
                            coverage="complete",
                            files=[dict(path=notice.relative_to(source).as_posix(), sha256=P.file_hash(notice))],
                        )
                        for name in plan["components"]
                    }
                ),
            )
            report, notices = P.audit(plan)
            self.assertEqual(report["result"], "passed")
            with self.assertRaisesRegex(P.PackageError, "explicitly reviewed"):
                P.archive_contents(plan, report, notices, "public")
            files, descriptor = P.archive_contents(plan, report, notices, "public", (sdk_root,))
            certificate = P.public_certificate_fields(descriptor, files[P.BUILD_PROVENANCE])
            audit_path = self.base / "public-audit.json"
            cli = ["--source-root", str(source), "--build-root", str(build), "--purpose", "public"]
            cli += ["--public-sdk-root", sdk_root, "--report", str(audit_path)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(P.main(["audit", *cli]), 0)
            self.assertEqual(
                P.read_json(audit_path)["public_certificate_bindings"],
                {key: value for key, value in certificate.items() if key != "result"},
            )
            P.atomic_json(certificate_path, P.read_json(audit_path)["public_certificate_bindings"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    P.main(
                        [
                            "build",
                            *cli,
                            "--certification",
                            str(certificate_path),
                            "--output",
                            str(self.base / "no-cert.zip"),
                        ]
                    ),
                    2,
                )
            self.assertFalse((self.base / "no-cert.zip").exists())
            P.atomic_json(certificate_path, dict(certificate, builder=r"E:\secret"))
            with self.assertRaisesRegex(P.PackageError, "does not bind"):
                P.make_archive(build, source, self.base / "certificate-extra.zip", **kwargs)
            P.atomic_json(certificate_path, certificate)
            public = self.base / "public-fixture.zip"
            second = self.base / "public-identical.zip"
            internal = self.base / "internal-fixture.zip"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    P.main(["build", *cli, "--certification", str(certificate_path), "--output", str(public)]), 0
                )
            P.make_archive(build, source, second, **kwargs)
            P.make_archive(build, source, internal)
        self.assertEqual(public.read_bytes(), second.read_bytes())
        archived = P.verify_archive(public, P.file_hash(public))
        with zipfile.ZipFile(public) as archive, zipfile.ZipFile(internal) as local:
            contents = {name: archive.read(name) for name in archive.namelist() if name != P.MANIFEST}
            portable = contents[P.BUILD_PROVENANCE]
            self.assertEqual(local.read(P.BUILD_PROVENANCE), original)
            self.assertEqual(contents[P.ASSET_SEAL], (host / "runtime-assets.json").read_bytes())
            self.assertEqual(contents[P.ASSET_SEAL], local.read(P.ASSET_SEAL))
            self.assertEqual(archived["runtime_fingerprint"], P.verify_archive(internal)["runtime_fingerprint"])
            self.assertEqual(certificate[P.PARENT_MANIFEST], P.digest(original))
            self.assertTrue(P.matches_build_provenance(archived, portable, original))
            self.assertFalse(P.matches_build_provenance(archived, portable, original + b"\n"))
            self.assertNotIn(b"private-id", portable)
            self.assertNotIn(b"private-pci-id", portable)
            self.assertNotIn(b"private", portable)
            self.assertNotIn(b"houdini_root", portable)
            self.assertIn(b"Fixture GPU", portable)
            inert = self.base / archived["package_id"]
            archive.extractall(inert)
        self.assertEqual(P.verify_installed(inert), archived)

        def mutated_archive(name, mutate, refresh_certificate=False):
            mutated = dict(contents)
            manifest = dict(archived)
            mutate(mutated, manifest)
            manifest["files"] = P.file_records(mutated)
            if P.PUBLIC_BINDING in manifest:
                manifest[P.PUBLIC_BINDING] = P.public_payload_binding(manifest)
            if refresh_certificate:
                mutated[P.CERTIFICATION] = P.canonical(
                    P.public_certificate_fields(manifest, mutated[P.BUILD_PROVENANCE])
                )
                manifest["files"] = P.file_records(mutated)
            manifest.pop("package_id")
            manifest["package_id"] = P.digest(P.canonical(manifest))
            output = self.base / (name + ".zip")
            with zipfile.ZipFile(output, "w") as archive:
                for member, content in mutated.items():
                    archive.writestr(member, content)
                archive.writestr(P.MANIFEST, P.canonical(manifest))
            return output

        def changed_parent(files, manifest):
            value = json.loads(files[P.BUILD_PROVENANCE])
            value["public_provenance"][P.PARENT_MANIFEST] = "a" * 64
            files[P.BUILD_PROVENANCE] = P.canonical(value)

        def hidden_path(files, manifest):
            value = json.loads(files["provenance/source.lock.json"])
            value["diagnostic"] = r"artifact E:\builder\secret"
            files["provenance/source.lock.json"] = P.canonical(value)

        def changed_seal(files, manifest):
            files[P.ASSET_SEAL] = P.canonical(dict(recipe=dict(houdini_root=r"C:\Users\private\SDK")))

        with self.assertRaisesRegex(P.PackageError, "does not bind"):
            P.verify_archive(mutated_archive("changed-parent", changed_parent))
        with self.assertRaisesRegex(P.PackageError, "does not bind"):
            P.verify_archive(mutated_archive("changed-license", lambda files, _: files.update(LICENSE=b"tampered")))
        with self.assertRaisesRegex(P.PackageError, "builder path"):
            P.verify_archive(mutated_archive("hidden-path", hidden_path, refresh_certificate=True))
        with self.assertRaisesRegex(P.PackageError, "nonpersonal local SDK"):
            P.verify_archive(mutated_archive("personal-seal", changed_seal, refresh_certificate=True))
        with self.assertRaisesRegex(P.PackageError, "missing its public payload binding"):
            P.verify_archive(mutated_archive("missing-binding", lambda _, manifest: manifest.pop(P.PUBLIC_BINDING)))

    def test_equivalent_staged_roots_normalize_but_retained_fields_remain_hashed(self):
        build, source, host, provider, save = self.payload_fixture(with_descriptor=True)
        descriptor = host / P.HYDRA_DESCRIPTOR.removeprefix("host/")
        with (
            patch.object(P, "pe_imports", return_value=[]),
            patch.object(P, "sdk_dependency_record", return_value=provider),
        ):
            before = P.payload_plan(build, source)
            value = P.read_json(descriptor)
            plugin = value["Plugins"][0]
            plugin.update(
                Root="Z:/another/build-b",
                LibraryPath="Z:/another/build-b/hdCycles.dll",
                ResourcePath="Z:/another/build-b/resources",
            )
            plugin["Info"]["Types"]["HdCyclesPlugin"]["displayName"] = "Different development title"
            P.atomic_json(descriptor, value)
            save()
            equivalent = P.payload_plan(build, source)
            self.assertEqual(before["runtime_fingerprint"], equivalent["runtime_fingerprint"])
            self.assertEqual(before["runtime_overrides"], equivalent["runtime_overrides"])
            plugin["Info"]["Types"]["HdCyclesPlugin"]["priority"] = 1
            P.atomic_json(descriptor, value)
            save()
            retained_field_edit = P.payload_plan(build, source)
            self.assertNotEqual(before["runtime_fingerprint"], retained_field_edit["runtime_fingerprint"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report", type=Path, default=artifacts_root() / "release-package-unit.json")
    args, remaining = parser.parse_known_args()
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    root = Path(__file__).resolve().parents[1]
    source_files = [
        "scripts/release-package.py",
        "scripts/fetch-release-notices.py",
        "tests/release_package_test.py",
        "docs/dev/notes/native-release-packaging.md",
    ]
    P.atomic_json(
        args.report,
        dict(
            schema_version=1,
            result="passed" if program.result.wasSuccessful() else "failed",
            tests_run=program.result.testsRun,
            failures=len(program.result.failures),
            errors=len(program.result.errors),
            skipped=len(program.result.skipped),
            scope="Pure isolated packaging; no renderer/host execution",
            source_files=[dict(path=name, sha256=P.file_hash(root / name)) for name in source_files],
        ),
    )
    raise SystemExit(not program.result.wasSuccessful())
