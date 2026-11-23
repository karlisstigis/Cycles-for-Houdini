# SPDX-License-Identifier: Apache-2.0
"""Recoverability of source separation, locked triage, and installer launcher compatibility."""

import hashlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = runpy.run_path(str(ROOT / "scripts/patch-triage.py"))
RELEASE = runpy.run_path(str(ROOT / "scripts/release-source.py"))


class PatchMaintenanceTest(unittest.TestCase):
    def test_archive_recovers_every_removed_source_byte(self):
        manifest = json.loads((ROOT / "docs/experiment-archive.json").read_text(encoding="utf-8"))
        archived = subprocess.check_output(["git", "-C", str(ROOT), "archive", manifest["original_tree"]])
        with tarfile.open(fileobj=io.BytesIO(archived)) as source:
            for record in manifest["files"]:
                with self.subTest(path=record["path"]):
                    self.assertFalse((ROOT / record["path"]).is_file())
                    self.assertEqual(
                        hashlib.sha256(source.extractfile(record["path"]).read()).hexdigest(), record["sha256"]
                    )
        self.assertTrue((ROOT / "src/features/image_iris/image_iris.cpp").is_file())
        patch = "patches/0073-resampled-light-sampling.patch"
        self.assertIn(patch, json.loads((ROOT / "source.lock.json").read_text())["cycles"]["patches"])
        for name in ("pt_ris_nee.h", "pt_ris_nee_common.h"):
            path = "src/kernel/integrator/" + name
            self.assertIn("+++ b/" + path, (ROOT / patch).read_text(encoding="utf-8"))
            self.assertTrue((ROOT / "third_party/cycles" / path).is_file())

    def test_triage_covers_every_locked_patch_and_rejects_drift(self):
        data = TRIAGE["validate"](ROOT)
        self.assertEqual(TRIAGE["markdown"](data), (ROOT / "docs/patch-triage.md").read_text(encoding="utf-8"))
        self.assertEqual(
            len(data["patches"]), len(json.loads((ROOT / "source.lock.json").read_text())["cycles"]["patches"])
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "patches").mkdir()
            content = b"diff --git a/src/CMakeLists.txt b/src/CMakeLists.txt\n"
            row = dict(
                number=1,
                path="patches/0001-one.patch",
                sha256=hashlib.sha256(content).hexdigest(),
                disposition="upstreamable",
                classification="glue",
                rationale="The renderer correction addresses a generic consumed-data ownership defect.",
                next_action="Extract a minimal upstream reproducer and qualify both affected device backends.",
                retirement_condition="Require upstream equivalent and affected backend tests before removing this change.",
                related_fixture=None,
            )
            lock = dict(commit="base", patches=[row["path"]], patch_sha256={row["path"]: row["sha256"]})
            policy = dict(
                upstream="base", patches=[row], approved_features={}, upstream_queue={}, reviewed_exceptions={}
            )
            (root / "source.lock.json").write_text(json.dumps(dict(cycles=lock)))
            (root / row["path"]).write_bytes(content)
            (root / "patches/triage.json").write_text(json.dumps(policy))
            TRIAGE["validate"](root)
            (root / row["path"]).write_bytes(b"changed source\n")
            with self.assertRaisesRegex(RuntimeError, "source bytes changed"):
                TRIAGE["validate"](root)
            (root / row["path"]).write_bytes(content)
            (root / "patches/triage.json").write_text(json.dumps(dict(upstream="base", patches=[])))
            with self.assertRaisesRegex(RuntimeError, "complete locked stack"):
                TRIAGE["validate"](root)

    def test_parked_patch_decision_must_be_a_document_in_the_tree(self):
        for row in TRIAGE["validate"](ROOT)["retired_patches"]:
            self.assertTrue((ROOT / row["decision"]).is_file(), row["decision"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "patches").mkdir()
            (root / "docs").mkdir()
            (root / "docs/decision.md").write_text("# Decision\\n", encoding="utf-8")
            parked = dict(
                number=7,
                path="patches/0007-parked.patch",
                sha256="0" * 64,
                classification="retired/parked",
                experiments_branch="experiments/parked",
                integration_archive_commit="1" * 40,
                engine_archive_commit="2" * 40,
                decision="docs/decision.md",
            )
            lock = dict(commit="base", patches=[], patch_sha256={})
            (root / "source.lock.json").write_text(json.dumps(dict(cycles=lock)))
            for decision, valid in (
                ("docs/decision.md", True),
                ("docs/missing.md", False),
                ("notes/private-review.md", False),
            ):
                policy = dict(
                    upstream="base",
                    patches=[],
                    approved_features={},
                    upstream_queue={},
                    reviewed_exceptions={},
                    retired_patches=[dict(parked, decision=decision)],
                )
                (root / "patches/triage.json").write_text(json.dumps(policy))
                if valid:
                    TRIAGE["validate"](root)
                else:
                    with self.assertRaisesRegex(RuntimeError, "decision document"):
                        TRIAGE["validate"](root)

    def test_policy_rejects_unapproved_render_changes(self):
        check = TRIAGE["check_classification"]
        row = dict(path="patches/new.patch", sha256="abc", classification="glue")
        policy = dict(approved_features={"image-iris": "decision"}, upstream_queue={}, reviewed_exceptions={})
        for path in ("src/kernel/foo.h", "src/integrator/foo.cpp", "src/scene/object.cpp"):
            content = f"diff --git a/{path} b/{path}\n".encode()
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "classification"):
                check(policy, row, content)
        # An engine file renamed to a build path still requires classification.
        content = b"diff --git a/src/kernel/foo.h b/src/CMakeLists.txt\n"
        policy["reviewed_exceptions"][row["path"]] = dict(classification="glue", sha256="abc")
        with self.assertRaisesRegex(RuntimeError, "classification"):
            check(policy, row, content)
        seam = b"diff --git a/src/integrator/output.cpp b/src/integrator/output.cpp\n"
        with self.assertRaisesRegex(RuntimeError, "classification"):
            check(policy, row, seam)
        policy["reviewed_exceptions"][row["path"]]["scope"] = "integrator-session-output"
        check(policy, row, seam)
        with self.assertRaisesRegex(RuntimeError, "classification"):
            check(policy, row, content)
        for category in ("", "feature/unknown", "fix", "migration-review", "pending-measurement/ris"):
            row["classification"] = category
            with self.subTest(category=category), self.assertRaisesRegex(RuntimeError, "classification"):
                check(policy, row, content)
        row["classification"] = "feature/image-iris"
        check(policy, row, content)
        row["classification"] = "fix"
        policy["upstream_queue"][row["path"]] = "queued"
        check(policy, row, content)
        row.update(path="patches/0087-future.patch", classification="migration-review")
        policy["reviewed_exceptions"][row["path"]] = dict(classification="migration-review", sha256="abc")
        with self.assertRaisesRegex(RuntimeError, "classification"):
            check(policy, row, content)

    def test_integrator_grant_expires_for_rehashed_patch_or_rebase(self):
        source = json.loads((ROOT / "patches/triage.json").read_text())
        row = next(r for r in source["patches"] if r["number"] == 16)
        name = row["path"]
        grant = source["reviewed_exceptions"][name]["grant"]
        original = (ROOT / name).read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / grant).parent.mkdir(parents=True)
            (root / grant).write_bytes((ROOT / grant).read_bytes())
            (root / "patches").mkdir()
            for base, content, expires in (
                (source["upstream"], original, False),
                (source["upstream"], original + b"\n", True),
                ("a" * 40, original, True),
            ):
                digest = hashlib.sha256(content).hexdigest()
                record = dict(row, related_fixture=None, sha256=digest)
                policy = dict(
                    upstream=base,
                    patches=[record],
                    approved_features={},
                    upstream_queue={},
                    reviewed_exceptions={name: dict(source["reviewed_exceptions"][name], sha256=digest)},
                )
                (root / name).write_bytes(content)
                (root / "patches/triage.json").write_text(json.dumps(policy))
                (root / "source.lock.json").write_text(
                    json.dumps(dict(cycles=dict(commit=base, patches=[name], patch_sha256={name: digest})))
                )
                if expires:
                    with self.assertRaisesRegex(RuntimeError, "Expired integrator grant"):
                        TRIAGE["validate"](root)
                else:
                    TRIAGE["validate"](root)

    def test_k33_pair_and_adapter_are_recoverable_outside_production(self):
        data = TRIAGE["validate"](ROOT)
        parked = [row for row in data["retired_patches"] if row["number"] in {45, 53}]
        self.assertEqual({row["number"] for row in parked}, {45, 53})
        self.assertEqual(len(data["approved_features"]), 9)
        self.assertIn("interactive-light-tree-updates", data["approved_features"])
        for row in parked:
            content = subprocess.check_output(
                ["git", "-C", str(ROOT), "show", row["integration_archive_commit"] + ":" + row["path"]]
            )
            self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"])
            self.assertFalse((ROOT / row["path"]).exists())
        archive = parked[0]["integration_archive_commit"]
        adapter = subprocess.check_output(["git", "-C", str(ROOT), "show", archive + ":src/core/shader_graph.cpp"])
        self.assertIn(b"OwnedRampNode", adapter)
        engine_tip = parked[0]["engine_archive_commit"]
        engine_node = subprocess.check_output(
            ["git", "-C", str(ROOT / "third_party/cycles"), "show", engine_tip + ":src/scene/shader_nodes.h"]
        )
        self.assertIn(b"OwnedRampNode", engine_node)
        self.assertIn(b"curve_source", engine_node)

    def test_k22_color_pair_is_recoverable_and_alpha_successor_remains_locked(self):
        data = TRIAGE["validate"](ROOT)
        parked = [row for row in data["retired_patches"] if row["number"] in {11, 12}]
        self.assertEqual({row["number"] for row in parked}, {11, 12})
        self.assertIn(13, {row["number"] for row in data["patches"]})
        for row in parked:
            content = subprocess.check_output(
                ["git", "-C", str(ROOT), "show", row["integration_archive_commit"] + ":" + row["path"]]
            )
            self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"])
            self.assertFalse((ROOT / row["path"]).exists())
            header = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(ROOT / "third_party/cycles"),
                    "show",
                    row["engine_archive_commit"] + ":src/util/colorspace.h",
                ]
            )
            self.assertIn(b"set_configuration", header)

    def test_release_snapshot_includes_moved_production_launchers(self):
        names = {row["path"] for row in RELEASE["records"](ROOT)}
        self.assertIn("launchers/Build and make installer.cmd", names)
        self.assertIn("launchers/Refresh Cycles registration.cmd", names)
        # Every launcher in the checkout is carried, including any that are not in the public source tree.
        for launcher in (ROOT / "launchers").glob("*.cmd"):
            self.assertIn("launchers/" + launcher.name, names)


if __name__ == "__main__":
    unittest.main()
