# SPDX-License-Identifier: Apache-2.0
"""Source archives retain failed evidence and reject changed or unsafe input."""

from support.paths import artifacts_root

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import shutil

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_archive", root / "scripts/archive-native-verification.py")
archive_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive_module)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "artifacts/verification/run"
        self.run.mkdir(parents=True)
        self.destination = self.root / "artifacts/verification/candidate/evidence"
        self.source = self.root / "src/example.cpp"
        self.source.parent.mkdir()
        self.source.write_text("original source")
        self.manifest = dict(integration_source=[dict(path="src/example.cpp", sha256=self.hash(self.source))])
        self.write("build-manifest.json", self.manifest)
        self.write(
            "source-audit.json",
            dict(result="passed", build_manifest_sha256=self.hash(self.run / "build-manifest.json")),
        )
        self.summary = dict(result="passed", tests=[dict(test="example.py", result="passed", exit_code=0)])
        self.write("summary.json", self.summary)

    @staticmethod
    def hash(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def archive(self):
        return archive_module.archive(self.root, self.run, self.destination)

    def test_immutable_cache_survives_later_source_edits(self):
        record = self.archive()
        self.source.write_text("later edits")
        self.assertEqual(self.archive(), record)
        index = json.loads((self.destination / "index.json").read_text())
        self.assertEqual(len(index["runs"]), 1)
        saved = self.root / record["source_archive"] / "src/example.cpp"
        self.assertEqual(saved.read_text(), "original source")
        saved.write_text("corrupted archive")
        with self.assertRaisesRegex(ValueError, "Archived source identity"):
            self.archive()

    def test_failed_terminal_run_keeps_its_evidence(self):
        self.summary.update(result="failed", tests=[dict(test="example.py", result="failed", exit_code=1)])
        self.write("summary.json", self.summary)
        self.assertEqual(self.archive()["result"], "failed")

    def test_changed_source_does_not_publish(self):
        self.source.write_text("edited before archival")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.archive()
        self.assertFalse((self.destination / "index.json").exists())
        self.assertFalse(any(self.destination.glob("sources-*")))

    def test_nonterminal_and_failed_audits_are_rejected(self):
        self.summary["tests"][0]["exit_code"] = None
        self.write("summary.json", self.summary)
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.archive()
        self.summary["tests"][0]["exit_code"] = 0
        self.write("summary.json", self.summary)
        self.write("source-audit.json", dict(result="failed"))
        with self.assertRaisesRegex(ValueError, "source audit"):
            self.archive()

    def test_paths_cannot_escape_workspace_or_run(self):
        with self.assertRaises(ValueError):
            archive_module.archive(self.root, self.run, self.root / "outside")
        with self.assertRaises(ValueError):
            archive_module.archive(self.root, self.run, self.run / "sources")
        with self.assertRaises(ValueError):
            archive_module.source_path(self.root, "../outside.cpp")
        with self.assertRaises(ValueError):
            archive_module.source_path(self.root, str(self.source))

    def test_external_evidence_requires_explicit_boundary_and_retains_absolute_lookup(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        external = Path(temporary.name).resolve()
        run = external / "runs/one"
        destination = external / "archives"
        shutil.copytree(self.run, run)
        with self.assertRaises(ValueError):
            archive_module.archive(self.root, run, destination)
        record = archive_module.archive(self.root, run, destination, evidence_root=external)
        self.assertEqual(Path(record["run"]), run)
        self.assertEqual((Path(record["source_archive"]) / "src/example.cpp").read_text(), "original source")
        with self.assertRaises(ValueError):
            archive_module.archive(self.root, run, self.root / "escape", evidence_root=external)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ArchiveTests))
    path = artifacts_root() / "stage0/native-verification-archive/report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            dict(
                result="passed" if result.wasSuccessful() else "failed",
                checks=result.testsRun,
                failures=len(result.failures),
                errors=len(result.errors),
            ),
            indent=2,
        )
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
