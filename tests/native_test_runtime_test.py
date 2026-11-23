# SPDX-License-Identifier: Apache-2.0
"""Pure tests for test subprocess provenance and fixed-path regression guards."""

from support.paths import artifacts_root
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_test_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # The Windows sandbox's virtual temp directory does not permit atomic
        # replacement of an existing target. The owned workspace temp root
        # exercises the same real os.replace operation used by publication.
        temporary_root = artifacts_root() / "unit-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.build = Path(self.temporary.name) / "custom-build-location"
        (self.build / "bin").mkdir(parents=True)
        (self.build / "hosts/test").mkdir(parents=True)
        self.executable = self.build / "bin/ch_harness.exe"
        self.executable.write_bytes(b"explicit selected harness")
        self.manifest = self.build / "build-manifest.json"
        self.data = dict(
            host_sdk=dict(houdini="22.0.466"),
            binary_artifacts=[
                dict(path="bin/ch_harness.exe", sha256=hashlib.sha256(self.executable.read_bytes()).hexdigest())
            ],
        )
        self.manifest.write_text(json.dumps(self.data))
        self.context = dict(
            build_directory=str(self.build),
            manifest=str(self.manifest),
            host=str(self.build / "hosts/test"),
            houdini="22.0.466",
            manifest_sha256=hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
        )
        self.environment = patch.dict(os.environ, CYCLES_HOUDINI_TEST_IDENTITY=json.dumps(self.context))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def update_context(self):
        os.environ["CYCLES_HOUDINI_TEST_IDENTITY"] = json.dumps(self.context)

    def test_custom_location(self):
        self.assertEqual(runtime.build_directory(), self.build)
        self.assertEqual(runtime.build_manifest(), self.manifest)
        self.assertEqual(runtime.runtime_executable(), self.executable)

    def test_missing_context_has_no_historical_fallback(self):
        del os.environ["CYCLES_HOUDINI_TEST_IDENTITY"]
        with self.assertRaisesRegex(RuntimeError, "Missing verified test runtime"):
            runtime.runtime_executable()

    def test_different_houdini_rejected(self):
        self.context["houdini"] = "99.1.2"
        self.update_context()
        with self.assertRaisesRegex(RuntimeError, "versions differ"):
            runtime.runtime_executable()

    def test_altered_binary_rejected_on_every_lookup(self):
        runtime.runtime_executable()
        self.executable.write_bytes(b"replaced after first lookup")
        with self.assertRaisesRegex(RuntimeError, "differs from selected build"):
            runtime.runtime_executable()

    def test_changed_manifest_rejected(self):
        self.data["binary_artifacts"].append(self.data["binary_artifacts"][0])
        self.manifest.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(RuntimeError, "changed after runtime selection"):
            runtime.runtime_executable()

    def test_manifest_replacement_cannot_authorize_replacement_binary(self):
        # Deterministically replace the path immediately after its verified
        # bytes are read. The previous helper would then reread the attacker's
        # records and authorize the replacement executable under a stale hash.
        original_read = Path.read_bytes
        reads = []
        replaced_binary = b"replaced binary not selected by launcher"
        changed = json.loads(json.dumps(self.data))
        changed["binary_artifacts"][0]["sha256"] = hashlib.sha256(replaced_binary).hexdigest()
        replacement = self.manifest.with_suffix(".replacement.json")
        replacement.write_text(json.dumps(changed))

        def replace_after_read(path):
            payload = original_read(path)
            if path == self.manifest:
                reads.append(payload)
                replacement.replace(self.manifest)
                self.executable.write_bytes(replaced_binary)
            return payload

        with patch.object(Path, "read_bytes", replace_after_read):
            with self.assertRaisesRegex(RuntimeError, "executable differs from selected build"):
                runtime.runtime_executable()
        self.assertEqual(len(reads), 1, "Manifest must be read once per executable authorization")

    def test_unverified_manifest_is_not_parsed(self):
        self.manifest.write_bytes(b"invalid JSON from a different manifest")
        with self.assertRaisesRegex(RuntimeError, "changed after runtime selection"):
            runtime.runtime_executable()

    def test_duplicate_artifact_rejected(self):
        self.data["binary_artifacts"].append(self.data["binary_artifacts"][0])
        self.manifest.write_text(json.dumps(self.data))
        self.context["manifest_sha256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.update_context()
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            runtime.runtime_executable()

    def test_unmanifested_auxiliary_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            runtime.runtime_executable("cycles.exe")

    def test_external_manifest_rejected(self):
        self.context["manifest"] = str(self.build.parent / "build-manifest.json")
        self.update_context()
        with self.assertRaisesRegex(RuntimeError, "outside the selected build"):
            runtime.runtime_executable()

    def test_relative_build_rejected(self):
        self.context["build_directory"] = "build/something"
        self.update_context()
        with self.assertRaisesRegex(RuntimeError, "absolute paths"):
            runtime.runtime_executable()

    def test_relative_host_rejected(self):
        self.context["host"] = "relative/host"
        self.update_context()
        with self.assertRaisesRegex(RuntimeError, "absolute paths"):
            runtime.runtime_executable()

    def test_missing_or_file_host_rejected(self):
        for host in (self.build / "missing-host", self.executable):
            with self.subTest(host=host):
                self.context["host"] = str(host)
                self.update_context()
                with self.assertRaisesRegex(RuntimeError, "existing directories"):
                    runtime.runtime_executable()

    def test_manifest_directory_rejected(self):
        self.manifest.unlink()
        self.manifest.mkdir()
        with self.assertRaisesRegex(RuntimeError, "manifest must be an existing file"):
            runtime.runtime_executable()

    def test_executable_directory_rejected(self):
        self.executable.unlink()
        self.executable.mkdir()
        with self.assertRaisesRegex(RuntimeError, "executable must be an existing file"):
            runtime.runtime_executable()

    def test_unapproved_name_rejected(self):
        with self.assertRaises(ValueError):
            runtime.runtime_executable("../elsewhere.exe")

    def test_maintained_fixtures_have_no_fixed_build_dispatch(self):
        # Synthetic packaging fixtures may use any directory name. Executable
        # dispatch and runtime evidence must follow the selected context.
        root = Path(__file__).resolve().parent
        violations = []
        for file in root.glob("*.py"):
            if file.name in ("runtime_assets_test.py", Path(__file__).name):
                continue
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "build/windows-h22-release" in node.value or "Houdini 22.0.429/" in node.value:
                        violations.append(f"{file.name}:{node.lineno}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
