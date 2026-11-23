# SPDX-License-Identifier: Apache-2.0
"""Snapshot baseline preservation and future-only patch export contracts."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history = load("source_history", "source-history.py")
exporter = load("export_engine_patches", "export-engine-patches.py")


class SourceHistoryTest(unittest.TestCase):
    def test_exports_keep_patch_ids_after_middle_and_tail_retirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = dict(patches=["patches/0001-first.patch", "patches/0003-third.patch"])
            self.assertEqual(exporter.next_patch_number(root, engine), 4)
            (root / "patches").mkdir()
            (root / "patches/triage.json").write_text(
                json.dumps(dict(retired_patches=[dict(path="patches/0005-parked.patch")]))
            )
            self.assertEqual(exporter.next_patch_number(root, engine), 6)

    def setUp(self):
        # Hython deliberately isolates user Git configuration. Keep identity
        # local to these disposable fixture subprocesses, never the real tool.
        identity = patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_NAME": "History fixture",
                "GIT_AUTHOR_EMAIL": "history-fixture@example.invalid",
                "GIT_COMMITTER_NAME": "History fixture",
                "GIT_COMMITTER_EMAIL": "history-fixture@example.invalid",
            },
        )
        identity.start()
        self.addCleanup(identity.stop)

    def test_export_moves_authoring_tree_with_authoring_tip(self):
        # The public build route checks the replayed engine against cycles.authoring_tree;
        # an export that moves authoring_tip must record the new tip's tree beside it.
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary) / "source", Path(temporary) / "engine"
            (root / "patches").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "tests/engine_feature_test.py").write_text("# fixture\n", encoding="utf-8")
            repo.mkdir()

            def engine_git(*args):
                return exporter.git(repo, *args).decode().strip()

            engine_git("init", "-q")
            (repo / "kernel.h").write_text("base\n", encoding="utf-8")
            engine_git("add", "kernel.h")
            engine_git("commit", "-q", "-m", "upstream base")
            base = engine_git("rev-parse", "HEAD")
            (repo / "kernel.h").write_text("base\nlocked\n", encoding="utf-8")
            engine_git("commit", "-q", "-am", "locked change")
            locked_tip = engine_git("rev-parse", "HEAD")
            locked_patch = root / "patches/0001-locked-change.patch"
            locked_patch.write_bytes(exporter.git(repo, "format-patch", "--stdout", "-1", "HEAD"))
            digest = hashlib.sha256(locked_patch.read_bytes()).hexdigest()
            (repo / "kernel.h").write_text("base\nlocked\nfuture\n", encoding="utf-8")
            message = "future change\n\nRationale: fixture\nContract: fixture\nTests: tests/engine_feature_test.py\n"
            engine_git("commit", "-q", "-am", message)
            new_tip, new_tree = engine_git("rev-parse", "HEAD"), engine_git("rev-parse", "HEAD^{tree}")
            for previous_tree in (engine_git("rev-parse", locked_tip + "^{tree}"), None):
                cycles = dict(
                    commit=base,
                    authoring_tip=locked_tip,
                    authoring_tree=previous_tree,
                    patches=["patches/0001-locked-change.patch"],
                    patch_sha256={"patches/0001-locked-change.patch": digest},
                )
                if previous_tree is None:
                    del cycles["authoring_tree"]
                (root / "source.lock.json").write_text(json.dumps(dict(cycles=cycles)), encoding="utf-8")
                output = Path(temporary) / ("export-" + str(previous_tree is None))
                report = exporter.export(root, repo, output)
                proposal = json.loads((output / "source.lock.proposed.json").read_text(encoding="utf-8"))["cycles"]
                self.assertEqual((report["tip"], report["authoring_tree"]), (new_tip, new_tree))
                self.assertEqual((proposal["authoring_tip"], proposal["authoring_tree"]), (new_tip, new_tree))
                keys = list(proposal)
                self.assertEqual(keys[keys.index("authoring_tip") + 1], "authoring_tree")
                self.assertEqual(
                    proposal["patches"], ["patches/0001-locked-change.patch", "patches/0002-future-change.patch"]
                )

    def test_recovery_uses_trusted_verifier_and_preserves_intent_to_add(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "integration"
            root.mkdir()
            history.git(parent, "init", "-q", str(root))
            history.configure(root)
            (root / ".gitignore").write_bytes(b"/third_party/\n")
            (root / "scripts").mkdir()
            (root / "scripts/engine_source.py").write_bytes(b"raise AssertionError('archive code executed')\n")
            history.git(root, "add", ".")
            history.git(
                root,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "integration",
            )
            production = root / "third_party/cycles"
            production.mkdir(parents=True)
            history.git(root, "init", "-q", str(production))
            history.configure(production)
            (production / "src").mkdir()
            (production / "src/a.cpp").write_bytes(b"base\n")
            history.git(production, "add", ".")
            history.git(
                production,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "base",
            )
            author = parent / "author"
            history.git(parent, "clone", "-q", "--no-checkout", str(production), str(author))
            history.configure(author)
            history.git(author, "checkout", "-q", "HEAD")
            history.git(author, "config", "user.name", "fixture")
            history.git(author, "config", "user.email", "fixture@example.invalid")
            history.git(author, "checkout", "-qb", "feature/authoring")
            (author / "src/a.cpp").write_bytes(b"feature\n")
            history.git(author, "commit", "-qam", "feature branch work")
            (production / "src/a.cpp").write_bytes(b"edited\r\n")
            (production / "src/intent.cpp").write_bytes(b"intent\r\n")
            history.git(production, "add", "-N", "--", "src/intent.cpp")
            (production / "src/untracked.h").write_bytes(b"untracked\r\n")
            archive = parent / "recovery.zip"
            history.recovery(root, author, archive)
            report = history.restore_recovery(archive, parent / "restored")
            self.assertTrue(report["production_engine_fingerprint_matches"])
            restored = parent / "restored/integration/third_party/cycles"
            self.assertEqual(history.ENGINE_FINGERPRINT(restored), history.ENGINE_FINGERPRINT(production))
            self.assertEqual((restored / "src/a.cpp").read_bytes(), b"edited\r\n")
            self.assertEqual(history.head_ref(parent / "restored/engine-authoring"), "refs/heads/feature/authoring")
            history.git(author, "checkout", "-q", "--detach", "HEAD")
            (author / "src/a.cpp").write_bytes(b"detached and unreachable\n")
            history.git(author, "commit", "-qam", "detached work beyond named refs")
            detached = history.git(author, "rev-parse", "HEAD").decode().strip()
            detached_archive = parent / "detached.zip"
            integration_tip = history.git(root, "rev-parse", "HEAD").decode().strip()
            history.git(root, "update-ref", "refs/proposals/local-review", integration_tip)
            history.recovery(root, author, detached_archive, integration_tip)
            detached_report = history.restore_recovery(detached_archive, parent / "restored-detached")
            integration = parent / "restored-detached/integration"
            self.assertIsNone(history.head_ref(integration))
            self.assertEqual(
                history.git(integration, "rev-parse", "refs/proposals/local-review").decode().strip(), integration_tip
            )
            self.assertEqual(detached_report["engine_head"], detached)
            self.assertIsNone(history.head_ref(parent / "restored-detached/engine-authoring"))
            self.assertEqual(
                (parent / "restored-detached/engine-authoring/src/a.cpp").read_bytes(), b"detached and unreachable\n"
            )
            # A production fork can advance on a different branch from the
            # preserved companion. Its commit objects need their own pack.
            history.git(production, "add", ".")
            history.git(
                production,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "production-only source",
            )
            production_tip = history.git(production, "rev-parse", "HEAD").decode().strip()
            history.git(
                production, "notes", "--ref=cycles-houdini", "add", "-m", "Historical release evidence", "HEAD^"
            )
            independent = parent / "independent-production.zip"
            history.recovery(root, author, independent)
            history.restore_recovery(independent, parent / "restored-independent")
            independent_engine = parent / "restored-independent/integration/third_party/cycles"
            self.assertEqual(history.git(independent_engine, "rev-parse", "HEAD").decode().strip(), production_tip)
            self.assertEqual(history.ENGINE_FINGERPRINT(independent_engine), history.ENGINE_FINGERPRINT(production))
            self.assertEqual(
                history.git(independent_engine, "rev-parse", history.PRODUCTION_NOTES),
                history.git(production, "rev-parse", history.PRODUCTION_NOTES),
            )

    def test_source_paths_reject_git_administration_and_windows_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                "../outside",
                "/absolute",
                "src/.git/config",
                "src/.GIT/config",
                "src/.git./config",
                "src/.git /config",
                "src\\.git\\config",
                "C:/outside",
            ):
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    history.safe_source_path(root, name)
            self.assertEqual(history.safe_source_path(root, "src/allowed.h"), root / "src/allowed.h")

    def test_snapshot_baseline_preserves_dirty_files_and_ignored_asset_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            (root / ".gitignore").write_bytes(b"*.png\n/artifacts/\n")
            (root / "source.txt").write_bytes(b"new concurrent edit\r\n")
            asset = b"\x89PNG\r\n\x00\x1a\n"
            files = {"source.txt": b"accepted release\r\n", "branding.png": asset}
            snapshot = root / "artifacts/source.zip"
            with zipfile.ZipFile(snapshot, "w") as archive:
                archive.writestr(
                    "snapshot.json",
                    json.dumps(
                        {
                            "integration_source": [
                                dict(path=name, sha256=history.sha(data)) for name, data in files.items()
                            ]
                        }
                    ),
                )
                for name, data in files.items():
                    archive.writestr("integration/" + name, data)
            report = history.init_integration(root, snapshot)
            self.assertEqual(report["integration_inputs"], 2)
            self.assertEqual((root / "source.txt").read_bytes(), b"new concurrent edit\r\n")
            for name, content in files.items():
                self.assertEqual(history.git(root, "show", "HEAD:" + name), content)
            self.assertIn(b"branding.png", history.git(root, "ls-files", "-ci", "--exclude-standard"))
            with self.assertRaises(RuntimeError):
                history.init_integration(root, snapshot)

    def test_export_future_commit_preserves_accepted_patch_and_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "engine"
            history.git(root, "init", "-q", str(repo))
            history.configure(repo)
            history.git(repo, "config", "user.name", "History test")
            history.git(repo, "config", "user.email", "history-test@example.invalid")
            file = repo / "engine.cpp"
            file.write_bytes(b"base\n")
            history.git(repo, "add", ".")
            history.git(repo, "commit", "-qm", "base")
            base = history.git(repo, "rev-parse", "HEAD").decode().strip()
            file.write_bytes(b"accepted\n")
            history.git(repo, "commit", "-qam", "accepted")
            accepted = history.git(repo, "rev-parse", "HEAD").decode().strip()
            (root / "patches").mkdir()
            locked = root / "patches/0001-accepted.patch"
            locked.write_bytes(b"accepted historical patch bytes\r\n")
            lock = {"cycles": {"commit": base, "authoring_tip": accepted, "patches": ["patches/" + locked.name]}}
            (root / "source.lock.json").write_text(json.dumps(lock), encoding="utf-8")
            file.write_bytes(b"future\n")
            history.git(repo, "commit", "-qam", "future change")
            output = root / "export"
            with self.assertRaisesRegex(RuntimeError, "Rationale"):
                exporter.export(root, repo, output)
            self.assertFalse(output.exists())
            (root / "tests").mkdir()
            (root / "tests/future_test.py").write_text("assert True\n")
            history.git(
                repo,
                "commit",
                "--amend",
                "-qm",
                "future change\n\nRationale: Preserve future scene ownership through the renderer update.\n"
                "Contract: The updated source retains the accepted observable behavior.\n"
                "Tests: tests/future_test.py",
            )
            report = exporter.export(root, repo, output)
            self.assertEqual(report["exported"], 1)
            self.assertTrue(report["commit_metadata_validated"])
            self.assertTrue(report["patches"][0]["path"].startswith("patches/0002-"))
            self.assertEqual(locked.read_bytes(), b"accepted historical patch bytes\r\n")
            proposal = json.loads((output / "source.lock.proposed.json").read_text())
            self.assertEqual(proposal["cycles"]["patches"][0], lock["cycles"]["patches"][0])
            self.assertEqual(proposal["cycles"]["authoring_tip"], report["tip"])
            self.assertEqual(
                proposal["cycles"]["patch_sha256"],
                {
                    lock["cycles"]["patches"][0]: history.sha(locked.read_bytes()),
                    report["patches"][0]["path"]: history.sha((output / report["patches"][0]["path"]).read_bytes()),
                },
            )
            replay = root / "replay"
            history.git(root, "clone", "-q", "--no-checkout", str(repo), str(replay))
            history.configure(replay)
            history.git(replay, "checkout", "-q", accepted)
            patch = output / report["patches"][0]["path"]
            history.git(replay, "apply", "--index", str(patch))
            self.assertEqual((replay / "engine.cpp").read_bytes(), b"future\n")
            with self.assertRaises(RuntimeError):
                exporter.export(root, repo, output)
            file.write_bytes(b"uncommitted\n")
            with self.assertRaises(RuntimeError):
                exporter.export(root, repo, root / "dirty-export")


if __name__ == "__main__":
    unittest.main()
