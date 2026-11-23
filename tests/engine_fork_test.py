# SPDX-License-Identifier: Apache-2.0
"""Production fork replay, exact legacy provenance and rollback without dependency moves."""

import hashlib
import json
import io
from contextlib import redirect_stderr
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FORK = runpy.run_path(str(ROOT / "scripts/engine-fork.py"))
IDENTITY = runpy.run_path(str(ROOT / "scripts/engine_source.py"))
HISTORY = runpy.run_path(str(ROOT / "scripts/source-history.py"))


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def sha(data):
    return hashlib.sha256(data).hexdigest()


class EngineForkTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            dict(
                GIT_AUTHOR_NAME="fixture",
                GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="fixture",
                GIT_COMMITTER_EMAIL="fixture@example.invalid",
            ),
        )
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.production = self.root / "third_party/cycles"
        self.production.mkdir(parents=True)
        git(self.root, "init", "-q", str(self.production))
        git(self.production, "config", "core.autocrlf", "false")
        (self.production / "src").mkdir()
        (self.production / "src/a.cpp").write_bytes(b"base\n")
        (self.production / ".gitignore").write_bytes(b"/lib/\n")
        git(self.production, "add", ".")
        git(self.production, "commit", "-qm", "base")
        self.base = git(self.production, "rev-parse", "HEAD").decode().strip()
        self.author = self.root / "author"
        git(self.root, "clone", "-q", "--no-checkout", str(self.production), str(self.author))
        git(self.author, "config", "core.autocrlf", "false")
        git(self.author, "checkout", "-q", "HEAD")
        (self.author / "src/a.cpp").write_bytes(b"feature\n")
        (self.author / "src/intent.h").write_bytes(b"intent\n")
        (self.author / "src/untracked.h").write_bytes(b"untracked\n")
        git(self.author, "add", "-N", ".")
        content = git(self.author, "diff", "--binary", "HEAD")
        (self.root / "patches").mkdir()
        self.name = "patches/0001-feature.patch"
        (self.root / self.name).write_bytes(content)
        git(self.author, "add", ".")
        git(
            self.author,
            "commit",
            "-qm",
            "Feature\n\nLocked-Patch: "
            + self.name
            + "\nLocked-Patch-SHA256: "
            + sha(content)
            + "\nContract: docs/renderer-patch-contracts.md\nRelated-Fixture: tests/engine_fork_test.py",
        )
        self.tip = git(self.author, "rev-parse", "HEAD").decode().strip()
        lock = dict(cycles=dict(commit=self.base, authoring_tip=self.tip, patches=[self.name]))
        (self.root / "source.lock.json").write_text(json.dumps(lock))
        (self.root / "patches/inventory.json").write_text(
            json.dumps(dict(patches=[dict(path=self.name, sha256=sha(content))]))
        )
        (self.production / "src/a.cpp").write_bytes(b"feature\r\n")
        for name in ("intent.h", "untracked.h"):
            (self.production / "src" / name).write_bytes((self.author / "src" / name).read_bytes())
        git(self.production, "add", "-N", "src/intent.h")
        dependency = self.production / "lib/windows_x64"
        dependency.mkdir(parents=True)
        self.sentinel = dependency / "sdk.lib"
        self.sentinel.write_bytes(b"precompiled library\x00")
        self.before = IDENTITY["fingerprint"](self.production)
        layout = dict(
            upstream=self.base,
            fork_tip=self.tip,
            baseline_fingerprint=self.before,
            crlf_files=[dict(path="src/a.cpp", fork_sha256=sha(b"feature\n"), legacy_sha256=sha(b"feature\r\n"))],
        )
        self.layout = self.root / "patches/legacy-source-layout.json"
        self.layout.write_text(json.dumps(layout))

    def test_materialized_fork_and_snapshot_keep_exact_commits(self):
        result = FORK["materialize"](self.root, self.author, self.root / "new-engine")
        self.assertEqual(result["tip"], self.tip)
        self.assertEqual(result["commits"], 1)
        self.assertEqual(IDENTITY["fingerprint"](self.production), self.before)
        with self.assertRaisesRegex(RuntimeError, "Destination exists"):
            FORK["materialize"](self.root, self.author, self.root / "new-engine")
        # Snapshot the clean fork at the canonical path in a separate root.
        restored_root = self.root / "snapshot-source"
        restored_root.mkdir()
        for name in ("source.lock.json",):
            (restored_root / name).write_bytes((self.root / name).read_bytes())
        shutil.copytree(self.root / "patches", restored_root / "patches")
        FORK["materialize"](restored_root, self.author, restored_root / "third_party/cycles")
        release = runpy.run_path(str(ROOT / "scripts/release-source.py"))
        archive = self.root / "snapshot.zip"
        with self.assertRaisesRegex(RuntimeError, "required recovery inputs"):
            release["snapshot"](restored_root, archive)
        for name in ("START HERE - Build Cycles for Houdini.cmd", "build-identity.json"):
            shutil.copyfile(ROOT / name, restored_root / name)
        shutil.copytree(ROOT / "blender", restored_root / "blender")
        release["snapshot"](restored_root, archive)
        self.assertGreater(release["check_snapshot"](restored_root, archive)["files"], 8)
        with zipfile.ZipFile(archive) as source:
            self.assertIn("integration/blender/worker_main.py", source.namelist())
            self.assertIn("integration/blender/cycles_blender/worker_server.py", source.namelist())
        report = HISTORY["restore_engine_snapshot"](restored_root, archive, self.root / "offline-engine")
        self.assertEqual(report["tip"], self.tip)
        self.assertEqual(git(self.root / "offline-engine", "rev-parse", "HEAD").decode().strip(), self.tip)

    def test_adoption_reproduces_legacy_hashes_and_preserves_libraries(self):
        self.assertEqual(IDENTITY["legacy_fingerprint"](self.author, self.base, self.before, self.layout), self.before)
        report = FORK["adopt"](self.root, self.author, self.production, self.root / "adoption")
        self.assertEqual(report["result"], "passed")
        self.assertFalse(IDENTITY["fingerprint"](self.production)["dirty"])
        self.assertEqual((self.production / "src/a.cpp").read_bytes(), b"feature\n")
        self.assertEqual(self.sentinel.read_bytes(), b"precompiled library\x00")
        self.assertEqual(
            IDENTITY["legacy_fingerprint"](self.production, self.base, self.before, self.layout), self.before
        )
        broken = dict(self.before, tracked_diff_sha256="0" * 64)
        with self.assertRaisesRegex(RuntimeError, "exact baseline"):
            IDENTITY["legacy_fingerprint"](self.production, self.base, broken, self.layout)

    def test_failed_adoption_restores_exact_source_index_and_head(self):
        original_verify = FORK["adopt"].__globals__["verify"]

        def fail_after_commit(root, repo, output=None):
            if repo == self.production:
                raise RuntimeError("injected post-migration failure")
            return original_verify(root, repo, output)

        old_head = git(self.production, "rev-parse", "HEAD")
        with patch.dict(FORK["adopt"].__globals__, verify=fail_after_commit):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                FORK["adopt"](self.root, self.author, self.production, self.root / "failed-adoption")
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), old_head)
        self.assertEqual(IDENTITY["fingerprint"](self.production), self.before)
        self.assertEqual((self.production / "src/a.cpp").read_bytes(), b"feature\r\n")
        self.assertEqual(self.sentinel.read_bytes(), b"precompiled library\x00")
        self.assertEqual(
            json.loads((self.root / "failed-adoption/migration.json").read_text())["result"], "failed-restored"
        )

    def test_fork_rejects_dirty_and_ignored_extra_source(self):
        (self.author / "src/a.cpp").write_bytes(b"drift\n")
        with self.assertRaisesRegex(RuntimeError, "clean"):
            FORK["verify"](self.root, self.author)
        (self.author / "src/a.cpp").write_bytes(b"feature\n")
        (self.author / ".git/info/exclude").write_text("src/hidden.h\n")
        (self.author / "src/hidden.h").write_text("hidden drift\n")
        with self.assertRaisesRegex(RuntimeError, "differs from replay"):
            FORK["verify"](self.root, self.author)

    def public_replay(self, name="public-engine"):
        """Recreate the fork commit on the upstream base, as git am does: new commit id, same tree."""
        public = self.root / name
        # No checkout before autocrlf is off: a Windows default would leave CRLF files that block checkout.
        git(self.root, "clone", "-q", "--no-checkout", "-c", "core.autocrlf=false", str(self.author), str(public))
        git(public, "checkout", "-q", "--detach", self.base)
        with patch.dict(os.environ, GIT_COMMITTER_DATE="2001-01-01T00:00:00Z"):
            git(public, "cherry-pick", "--allow-empty", self.tip)
        return public

    def test_public_replay_download_that_never_answers_is_retried_and_leaves_nothing(self):
        # The upstream fetch fails every time (here: a server that does not exist): tried FETCH_ATTEMPTS times with
        # growing pauses, then one plain sentence; the unfinished folder is removed, so the next run starts clean.
        names = FORK["materialize_public"].__globals__
        lock = dict(url="file://localhost/" + (self.root / "no-such-server").as_posix().lstrip("/"), commit=self.base,
                    authoring_tree="0" * 40, patches=[])
        pauses = []
        destination = self.root / "never-answered/cycles"
        with patch.dict(names, lock_engine=lambda root: lock, locked_patches=lambda root, engine: []), patch.object(
            names["time"], "sleep", pauses.append
        ), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, r"^The download from localhost did not answer, .*double-click the file again \(nothing is left half-installed\)\.$"):
                FORK["materialize_public"](self.root, destination)
        self.assertEqual(pauses, [5.0, 10.0, 20.0])
        self.assertFalse(destination.with_name("cycles.incomplete").exists())
        self.assertFalse(destination.exists())

    def test_public_replay_is_accepted_by_tree_only(self):
        tree = git(self.author, "rev-parse", "HEAD^{tree}").decode().strip()
        lock = dict(commit=self.base, authoring_tip=self.tip, authoring_tree=tree, patches=[self.name])
        public = self.public_replay()
        head = git(public, "rev-parse", "HEAD").decode().strip()
        self.assertNotEqual(head, self.tip)
        self.assertEqual(IDENTITY["engine_route"](self.author, lock), "private")
        self.assertEqual(IDENTITY["engine_route"](public, lock), "public")
        # The private route still needs the exact commit id; the public one records its own.
        self.assertEqual(IDENTITY["engine_identity"](self.author, lock)["expected_head"], self.tip)
        self.assertEqual(
            IDENTITY["engine_identity"](public, lock),
            dict(route="public", head=head, tree=tree, expected_head=head),
        )
        # Without a locked tree nothing but the exact tip is accepted.
        self.assertIsNone(IDENTITY["engine_route"](public, dict(lock, authoring_tree=None)))
        with self.assertRaisesRegex(RuntimeError, "matches neither route"):
            IDENTITY["engine_identity"](public, dict(lock, authoring_tree=None))
        # A modified engine (committed change, so the tree differs) passes neither route.
        (public / "src/a.cpp").write_bytes(b"modified\n")
        git(public, "commit", "-qam", "local change")
        self.assertIsNone(IDENTITY["engine_route"](public, lock))
        with self.assertRaisesRegex(RuntimeError, "matches neither route"):
            IDENTITY["engine_identity"](public, lock)
        # Upstream-only lock (no fork tip): the upstream commit is expected.
        upstream = IDENTITY["engine_identity"](self.production, dict(commit=self.base, patches=[]))
        self.assertEqual((upstream["route"], upstream["expected_head"]), ("upstream", self.base))

    def test_build_records_route_and_release_steps_refuse_public_route(self):
        report = (ROOT / "scripts/environment-report.py").read_text(encoding="utf-8")
        self.assertIn('engine_identity(roots["cycles"], lock["cycles"])', report)
        self.assertIn('cycles=engine["expected_head"]', report)
        self.assertIn('engine_route=engine["route"]', report)
        staging = (ROOT / "scripts/stage-development.ps1").read_text(encoding="utf-8")
        self.assertIn(
            "engine_route=$environmentReport.engine_route; engine_tree=$environmentReport.engine_tree;", staging
        )
        tree = git(self.author, "rev-parse", "HEAD^{tree}").decode().strip()
        lock = dict(commit=self.base, authoring_tip=self.tip, authoring_tree=tree, patches=[self.name])
        release_root = self.root / "release-source"
        (release_root / "third_party").mkdir(parents=True)
        (release_root / "source.lock.json").write_text(json.dumps(dict(cycles=lock)))
        public = self.public_replay("release-source/third_party/cycles")
        self.assertEqual(IDENTITY["engine_route"](public, lock), "public")
        release = runpy.run_path(str(ROOT / "scripts/release-source.py"))
        with self.assertRaisesRegex(RuntimeError, "only the private engine route"):
            release["snapshot"](release_root, self.root / "public-snapshot.zip")
        build = self.root / "public-build"
        build.mkdir()
        (build / "build-manifest.json").write_text(json.dumps(dict(engine_route="public")))
        with self.assertRaisesRegex(RuntimeError, "only the private engine route"):
            release["preservation"](build, None, self.root / "missing-package.zip")
        package = runpy.run_path(str(ROOT / "scripts/release-package.py"))
        with self.assertRaisesRegex(RuntimeError, "only from the private engine route"):
            package["fork_policy_files"](release_root, dict(engine_route="public", actual_revisions={}))


if __name__ == "__main__":
    unittest.main()
