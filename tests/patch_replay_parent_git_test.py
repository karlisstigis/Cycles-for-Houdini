# SPDX-License-Identifier: Apache-2.0
"""A replay under a parent repository must apply engine-relative paths."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("patch_verifier", ROOT / "scripts/verify-upstream-patches.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class ParentRepositoryReplayTest(unittest.TestCase):
    def test_nested_replay_changes_file_and_rejects_second_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            subprocess.run(["git", "init", "-q", str(parent)], check=True)
            tree = parent / "artifacts/replayed"
            (tree / "src").mkdir(parents=True)
            target = tree / "src/example.cpp"
            target.write_bytes(b"before\n")
            patch = parent / "change.patch"
            patch.write_bytes(
                (
                    "diff --git a/src/example.cpp b/src/example.cpp\n"
                    "--- a/src/example.cpp\n+++ b/src/example.cpp\n"
                    "@@ -1 +1 @@\n-before\n+after\n"
                ).encode()
            )
            verifier.apply_patch(tree, patch)
            self.assertEqual(target.read_bytes(), b"after\n")
            with self.assertRaises(subprocess.CalledProcessError):
                verifier.apply_patch(tree, patch)
            self.assertFalse((parent / "src").exists())

    def test_crlf_upstream_blob_and_original_patch_0081_under_parent_git(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            subprocess.run(["git", "init", "-q", str(parent)], check=True)
            engine = parent / "third_party/cycles"
            engine.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(engine)], check=True)
            subprocess.run(["git", "-C", str(engine), "config", "core.autocrlf", "false"], check=True)
            # Exercise CRLF deliberately, independently of the source checkout line endings.
            name = "patches/0081-gsplat-retained-attribute-copies.patch"
            patch_bytes = (ROOT / name).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            self.assertIn(b"\r\n", patch_bytes)
            hunk = patch_bytes.split(b"@@", 2)[2].split(b"\n", 1)[1].splitlines(keepends=True)
            before = b"".join(line[1:] for line in hunk if line[:1] in (b" ", b"-"))
            after = b"".join(line[1:] for line in hunk if line[:1] in (b" ", b"+"))
            target = engine / "src/scene/geometry_attributes.cpp"
            target.parent.mkdir(parents=True)
            target.write_bytes(before)
            binary = engine / "binary.dat"
            binary.write_bytes(b"\0binary\r\nunchanged")
            subprocess.run(["git", "-C", str(engine), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(engine),
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "CRLF baseline",
                ],
                check=True,
            )
            commit = subprocess.check_output(["git", "-C", str(engine), "rev-parse", "HEAD"]).decode().strip()
            target.write_bytes(after)
            patch = parent / name
            patch.parent.mkdir()
            patch.write_bytes(patch_bytes)
            (parent / "source.lock.json").write_text(json.dumps({"cycles": {"commit": commit, "patches": [name]}}))
            output = parent / "artifacts/replay"
            report = verifier.run(parent, output)
            self.assertEqual(report["result"], "passed")
            self.assertEqual(
                (output / "replayed/src/scene/geometry_attributes.cpp").read_bytes(), after.replace(b"\r\n", b"\n")
            )
            self.assertEqual((output / "replayed/binary.dat").read_bytes(), binary.read_bytes())
            self.assertEqual(patch.read_bytes(), patch_bytes)


if __name__ == "__main__":
    unittest.main()
