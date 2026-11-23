# SPDX-License-Identifier: Apache-2.0
"""Pure release gates: source additions/deletions, engine corruption and replay drift."""

from support.paths import artifacts_root

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RESTORE_FIXTURE_FILES = (
    "START HERE - Build Cycles for Houdini.cmd",
    "build-identity.json",
    "blender/LICENSE",
    "blender/worker_main.py",
    "blender/export_main.py",
    "blender/asset_prefs_main.py",
    "blender/cycles_blender/worker_server.py",
    "blender/cycles_blender/blend_export.py",
)


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


source = module("release-source")
replay = module("verify-upstream-patches")


class ReleaseContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=artifacts_root())
        self.addCleanup(self.cleanup_fixture)
        self.root = Path(self.temp.name)
        for name in RESTORE_FIXTURE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"Recovery fixture for {name}\n", encoding="utf-8")
        engine = self.root / "third_party/cycles"
        engine.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(engine)], check=True)
        (engine / "src").mkdir()
        (engine / "src/a.cpp").write_text("old\n")
        (engine / "unaffected.txt").write_text("unchanged\n")
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
                "baseline",
            ],
            check=True,
        )
        commit = subprocess.check_output(["git", "-C", str(engine), "rev-parse", "HEAD"], text=True).strip()
        (engine / "src/a.cpp").write_text("new\n")
        patch = subprocess.check_output(["git", "-C", str(engine), "diff"])
        (self.root / "patches").mkdir()
        (self.root / "patches/0001.patch").write_bytes(patch)
        (self.root / "source.lock.json").write_text(
            json.dumps(dict(cycles=dict(commit=commit, patches=["patches/0001.patch"])))
        )
        self.engine = engine

    def cleanup_fixture(self):
        # Windows scanners can briefly retain the just-used Git directory.
        # Retry sharing violations only; retain a persistent failure as a test error.
        for attempt in range(20):
            try:
                self.temp.cleanup()
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) != 32 or attempt == 19:
                    raise
                time.sleep(0.05)

    def test_extended_stack_requires_the_old_commit_at_the_old_prefix_length(self):
        def git(*args):
            return subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(self.engine),
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    *args,
                ],
                text=True,
            ).strip()

        upstream = git("rev-parse", "HEAD")
        git("commit", "-qam", "first renderer change")
        first = git("rev-parse", "HEAD")
        (self.engine / "src/a.cpp").write_text("next\n")
        git("commit", "-qam", "second renderer change")
        tip = git("rev-parse", "HEAD")
        self.assertTrue(source.matches_engine_prefix(self.engine, upstream, tip, first, 1, 2))
        self.assertFalse(source.matches_engine_prefix(self.engine, upstream, tip, first, 2, 2))
        self.assertFalse(source.matches_engine_prefix(self.engine, upstream, tip, tip, 1, 2))
        self.assertFalse(source.matches_engine_prefix(self.engine, upstream, tip, upstream, 1, 2))
        old_payload = dict(kernels=[dict(name="kernel_optix.ptx.zst", sha256="old")])
        new_payload = dict(kernels=[dict(name="kernel_optix.ptx.zst", sha256="new")], actual_revisions=dict(cycles=tip))
        self.assertTrue(source.kernel_payload_preserved(self.engine, old_payload, old_payload, None))
        self.assertFalse(source.kernel_payload_preserved(self.engine, old_payload, new_payload, None))
        # A host-only successor cannot account for changed GPU kernel bytes.
        self.assertFalse(source.kernel_payload_preserved(self.engine, old_payload, new_payload, first))
        (self.engine / "src/kernel").mkdir()
        (self.engine / "src/kernel/normal.h").write_text("correct static primitive classification\n")
        git("add", "src/kernel/normal.h")
        git("commit", "-qm", "kernel correction")
        new_payload["actual_revisions"]["cycles"] = git("rev-parse", "HEAD")
        self.assertTrue(source.kernel_payload_preserved(self.engine, old_payload, new_payload, first))
        new_payload["kernels"].clear()
        self.assertFalse(source.kernel_payload_preserved(self.engine, old_payload, new_payload, first))

    def test_rebase_receipt_expires_for_any_reviewed_input_change(self):
        subprocess.run(["git", "-C", str(self.engine), "checkout", "--", "src/a.cpp"], check=True)
        tip = subprocess.check_output(["git", "-C", str(self.engine), "rev-parse", "HEAD"], text=True).strip()
        baseline = self.root / "previous-manifest.json"
        baseline.write_bytes(b"immutable previous qualified manifest")
        name = "patches/rebase/approval.json"
        decision = "docs/decisions/0011-rebase-review.md"
        (self.root / decision).parent.mkdir(parents=True)
        (self.root / decision).write_text("Reviewed source transition")
        (self.root / name).parent.mkdir(parents=True)
        lock = dict(commit="new-base", authoring_tip=tip, patches=[], patch_sha256={}, release_rebase_approval=name)
        (self.root / "source.lock.json").write_text(json.dumps(dict(cycles=lock)))
        (self.root / "patches/triage.json").write_text(
            json.dumps(
                dict(upstream="new-base", patches=[], approved_features={}, upstream_queue={}, reviewed_exceptions={})
            )
        )
        receipt = dict(
            schema_version=1,
            result="approved",
            baseline_manifest_sha256=source.digest(baseline.read_bytes()),
            upstream="new-base",
            authoring_tip=tip,
            applied_patches=[],
            engine_fingerprint=source.engine_fingerprint(self.engine),
            decision=decision,
            decision_sha256=source.digest((self.root / decision).read_bytes()),
        )
        approval = self.root / name
        approval.write_text(json.dumps(receipt))
        self.assertEqual(source.approved_rebase(self.root, baseline, approval)["path"], name)
        for field, value in (
            ("result", "pending"),
            ("baseline_manifest_sha256", "0" * 64),
            ("upstream", "other-base"),
            ("authoring_tip", "other-tip"),
            ("applied_patches", [dict(path="unexpected", sha256="0" * 64)]),
            ("engine_fingerprint", {}),
            ("decision_sha256", "0" * 64),
        ):
            approval.write_text(json.dumps(dict(receipt, **{field: value})))
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "Expired or incomplete"):
                source.approved_rebase(self.root, baseline, approval)
        approval.write_text(json.dumps(receipt))
        (self.root / decision).write_text("Changed review after approval")
        with self.assertRaisesRegex(RuntimeError, "Expired or incomplete"):
            source.approved_rebase(self.root, baseline, approval)
        with self.assertRaisesRegex(RuntimeError, "requires an exact previous"):
            source.approved_rebase(self.root, None, approval)
        (self.engine / "src/a.cpp").write_text("unreviewed engine edit")
        with self.assertRaisesRegex(RuntimeError, "exact clean locked fork"):
            source.approved_rebase(self.root, baseline, approval)

    def test_full_replay_detects_untouched_engine_drift(self):
        self.assertEqual(replay.run(self.root, self.root / "clean")["result"], "passed")
        (self.engine / "unaffected.txt").write_text("unrecorded change\n")
        with self.assertRaises(RuntimeError):
            replay.run(self.root, self.root / "dirty")
        self.assertEqual(json.loads((self.root / "dirty/report.json").read_text())["changed"], ["unaffected.txt"])

    def test_snapshot_rejects_engine_changes_after_acceptance(self):
        archive = self.root / "snapshot.zip"
        source.snapshot(self.root, archive)
        (self.engine / "src/a.cpp").write_text("changed after acceptance\n")
        with self.assertRaisesRegex(RuntimeError, "Patched engine changed"):
            source.check_snapshot(self.root, archive)

    def test_old_snapshot_is_not_accepted_as_new_source_certification(self):
        archive = self.root / "old.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("snapshot.json", json.dumps(dict(schema_version=1)))
        with self.assertRaisesRegex(RuntimeError, "version 2 snapshot"):
            source.check_snapshot(self.root, archive)

    def test_current_payload_needs_no_prior_release_and_preserves_production_features(self):
        lock = json.loads((self.root / "source.lock.json").read_text())
        lock["certification_candidate"] = dict(houdini="22.0.466", cuda_architectures=["sm_86"])
        (self.root / "source.lock.json").write_text(json.dumps(lock))
        features = {
            name: "ON"
            for name in (
                "CH_IMAGE_IRIS",
                "WITH_DLSS",
                "WITH_CYCLES_DEVICE_CUDA",
                "WITH_CYCLES_DEVICE_OPTIX",
                "WITH_CYCLES_OPENIMAGEDENOISE",
                "WITH_CYCLES_OPENVDB",
                "WITH_CYCLES_NANOVDB",
                "WITH_CYCLES_OPENSUBDIV",
            )
        }
        features.update(
            {name: "OFF" for name in ("CH_BDPT_PROTOTYPE", "WITH_CYCLES_HYDRA_RENDER_DELEGATE", "WITH_CYCLES_USD")}
        )
        names = (
            "kernel_sm_86.cubin.zst",
            "kernel_optix.ptx.zst",
            "kernel_optix_mnee.ptx.zst",
            "kernel_optix_shader_raytrace.ptx.zst",
        )
        current = dict(
            host_sdk=dict(houdini="22.0.466"),
            features=features,
            actual_revisions=dict(cycles=lock["cycles"]["commit"]),
            source_fingerprint=dict(cycles=source.engine_fingerprint(self.engine)),
            applied_patches=[
                dict(path=name, sha256=source.digest((self.root / name).read_bytes()))
                for name in lock["cycles"]["patches"]
            ],
            kernels=[dict(name=name) for name in names],
        )
        build = self.root / "build"
        build.mkdir()
        manifest = build / "build-manifest.json"
        manifest.write_text(json.dumps(current))
        archive = self.root / "candidate.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(
                "release-manifest.json",
                json.dumps(dict(houdini="22.0.466", package_id="fixture", runtime_fingerprint="fixture")),
            )
        with patch.object(source, "ROOT", self.root):
            self.assertFalse(source.preservation(build, None, archive)["reference_comparison"])
            helper = module("release-package")
            frozen = self.root / "tested-manifest.json"
            frozen.write_bytes(manifest.read_bytes())
            portable = helper.portable_build_manifest(frozen.read_bytes())
            payload = {
                helper.BUILD_PROVENANCE: portable,
                helper.ASSET_SEAL: helper.canonical(dict(recipe=dict(houdini_root=r"D:\SDK\Houdini 22.0.466"))),
            }
            receipt = dict(
                schema_version=helper.SCHEMA,
                purpose="public",
                platform="windows-x64",
                houdini="22.0.466",
                runtime_fingerprint="fixture",
                certification="passed",
                audit={key: "passed" for key in ("build_sources", "dependencies", "notices")},
                files=helper.file_records(payload),
            )
            receipt[helper.PUBLIC_BINDING] = helper.public_payload_binding(receipt)
            payload[helper.CERTIFICATION] = helper.canonical(helper.public_certificate_fields(receipt, portable))
            receipt["files"] = helper.file_records(payload)
            receipt["package_id"] = helper.digest(helper.canonical(receipt))
            public_archive = self.root / "public-fixture.zip"
            with zipfile.ZipFile(public_archive, "w") as output:
                for name, value in payload.items():
                    output.writestr(name, value)
                output.writestr(helper.MANIFEST, helper.canonical(receipt))
            with patch.object(source, "load_release_package", return_value=helper):
                self.assertTrue(
                    source.preservation(build, None, public_archive, frozen)["checks"]["exact_tested_manifest"]
                )
                extracted = self.root / "public-reference.json"
                source.extract_baseline(public_archive, extracted, "22.0.466")
                self.assertEqual(extracted.read_bytes(), portable)
                changed = frozen.read_bytes() + b"\n"
                frozen.write_bytes(changed)
                manifest.write_bytes(changed)
                with self.assertRaisesRegex(RuntimeError, r'"exact_tested_manifest": false'):
                    source.preservation(build, None, public_archive, frozen)
                manifest.write_text(json.dumps(current))
            previous = json.loads(json.dumps(current))
            previous["actual_revisions"]["cycles"] = "previous-fork"
            previous["applied_patches"] = [dict(path="previous.patch", sha256="old")]
            baseline = self.root / "previous.json"
            baseline.write_text(json.dumps(previous))
            approval = self.root / "approval.json"
            approval.write_bytes(b"reviewed-rebase")
            with zipfile.ZipFile(archive, "a") as output:
                output.writestr("provenance/patches/approval.json", approval.read_bytes())
            with patch.object(source, "approved_rebase", return_value=dict(path="patches/approval.json")):
                self.assertEqual(
                    source.preservation(build, baseline, archive, rebase_approval=approval)["reference_mode"],
                    "approved-rebase",
                )
                for key, value, failure in (
                    ("host_sdk", dict(houdini="22.0.429"), "host_sdk"),
                    ("features", dict(features, WITH_DLSS="OFF"), "production_features"),
                    (
                        "actual_revisions",
                        dict(cycles=lock["cycles"]["commit"], dependencies="changed"),
                        "rebase_dependency_revisions",
                    ),
                    ("kernels", current["kernels"][:-1], "rebase_kernel_architectures"),
                ):
                    saved = current[key]
                    current[key] = value
                    manifest.write_text(json.dumps(current))
                    with self.subTest(rebase_invariant=key), self.assertRaisesRegex(RuntimeError, failure):
                        source.preservation(build, baseline, archive, rebase_approval=approval)
                    current[key] = saved
                manifest.write_text(json.dumps(current))
            # A new baseline still requires every locked patch, with exact
            # digest and ordering; retirement must not bypass this gate.
            second = "patches/0002.patch"
            (self.root / second).write_bytes(b"second retained production correction\n")
            lock["cycles"]["patches"].append(second)
            (self.root / "source.lock.json").write_text(json.dumps(lock))
            current["applied_patches"].append(
                dict(path=second, sha256=source.digest((self.root / second).read_bytes()))
            )
            retained = list(current["applied_patches"])
            manifest.write_text(json.dumps(current))
            self.assertFalse(source.preservation(build, None, archive)["reference_comparison"])
            for label, modified in (
                ("missing", retained[:-1]),
                ("changed", [dict(retained[0], sha256="0" * 64), retained[1]]),
                ("reordered", list(reversed(retained))),
            ):
                with self.subTest(patches=label):
                    current["applied_patches"] = modified
                    manifest.write_text(json.dumps(current))
                    with self.assertRaisesRegex(RuntimeError, '"locked_patches": false'):
                        source.preservation(build, None, archive)
            current["applied_patches"] = retained
            for feature in ("WITH_CYCLES_OPENVDB", "WITH_DLSS"):
                with self.subTest(feature=feature):
                    current["features"][feature] = "OFF"
                    manifest.write_text(json.dumps(current))
                    with self.assertRaisesRegex(RuntimeError, "production_features"):
                        source.preservation(build, None, archive)
                    current["features"][feature] = "ON"

    def test_replay_rejects_added_source_even_if_gitignored(self):
        (self.engine / ".git/info/exclude").write_text("src/hidden.cpp\n")
        (self.engine / "src/hidden.cpp").write_text("hidden source")
        with self.assertRaises(RuntimeError):
            replay.run(self.root, self.root / "hidden")
        self.assertIn("src/hidden.cpp", json.loads((self.root / "hidden/report.json").read_text())["extra"])

    def test_snapshot_detects_release_inputs_and_ignores_scratch(self):
        archive = self.root / "snapshot.zip"
        source.snapshot(self.root, archive)
        (self.root / "tmp").mkdir()
        (self.root / "tmp/scratch.py").write_text("temporary")
        source.check_snapshot(self.root, archive)
        (self.root / "scripts").mkdir()
        (self.root / "scripts/added.py").write_text("release input")
        with self.assertRaises(RuntimeError):
            source.check_snapshot(self.root, archive)

    def test_snapshot_requires_every_recovery_input(self):
        archive = self.root / "snapshot.zip"
        source.snapshot(self.root, archive)
        with zipfile.ZipFile(archive) as frozen:
            for name in RESTORE_FIXTURE_FILES:
                self.assertEqual(frozen.read("integration/" + name), (self.root / name).read_bytes())
        rejected = self.root / "incomplete.zip"
        for name in RESTORE_FIXTURE_FILES:
            path = self.root / name
            original = path.read_bytes()
            with self.subTest(name=name):
                path.unlink()
                try:
                    with self.assertRaisesRegex(RuntimeError, "Snapshot lacks required recovery inputs") as error:
                        source.snapshot(self.root, rejected)
                    self.assertIn(name, str(error.exception))
                    self.assertFalse(rejected.exists())
                finally:
                    path.write_bytes(original)

    def test_snapshot_verifier_rejects_a_missing_required_member_even_if_the_record_is_removed(self):
        archive = self.root / "snapshot.zip"
        source.snapshot(self.root, archive)
        rejected = self.root / "missing-worker.zip"
        missing = "blender/worker_main.py"
        with zipfile.ZipFile(archive) as frozen, zipfile.ZipFile(rejected, "w") as changed:
            metadata = json.loads(frozen.read("snapshot.json"))
            metadata["integration_source"] = [
                record for record in metadata["integration_source"] if record["path"] != missing
            ]
            for name in frozen.namelist():
                if name not in ("snapshot.json", "integration/" + missing):
                    changed.writestr(name, frozen.read(name))
            changed.writestr("snapshot.json", json.dumps(metadata))
        with self.assertRaisesRegex(RuntimeError, "Snapshot lacks required recovery inputs: blender/worker_main.py"):
            source.check_snapshot(self.root, rejected)

    def test_snapshot_engine_pack_restores_exact_git_identity(self):
        archive = self.root / "snapshot.zip"
        source.snapshot(self.root, archive)
        restored = self.root / "restored"
        restored.mkdir()
        subprocess.run(["git", "init", "-q", str(restored)], check=True)
        with zipfile.ZipFile(archive) as frozen:
            subprocess.run(
                ["git", "-C", str(restored), "unpack-objects", "-q"],
                input=frozen.read("pinned-engine.pack"),
                check=True,
            )
            metadata = json.loads(frozen.read("snapshot.json"))
        commit = metadata["engine_commit"]
        subprocess.run(["git", "-C", str(restored), "update-ref", "refs/heads/locked", commit], check=True)
        subprocess.run(["git", "-C", str(restored), "symbolic-ref", "HEAD", "refs/heads/locked"], check=True)
        subprocess.run(["git", "-C", str(restored), "reset", "--hard", "-q", "HEAD"], check=True)
        self.assertEqual((restored / "src/a.cpp").read_text(), "old\n")
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(restored), "rev-parse", "HEAD"], text=True).strip(), commit
        )

    def test_snapshot_rejects_corrupt_engine_archive(self):
        archive = self.root / "snapshot.zip"
        source.snapshot(self.root, archive)
        broken = self.root / "broken.zip"
        with zipfile.ZipFile(archive) as frozen, zipfile.ZipFile(broken, "w") as changed:
            for name in frozen.namelist():
                changed.writestr(name, b"corrupt" if name == "pinned-engine.pack" else frozen.read(name))
        with self.assertRaises(RuntimeError):
            source.check_snapshot(self.root, broken)

    def test_reference_extraction_verifies_payload_and_exact_host(self):
        helper = module("release-package")
        content = json.dumps(dict(host_sdk=dict(houdini="22.0.466"))).encode()
        name = "provenance/build-manifest.json"
        manifest = dict(
            schema_version=helper.SCHEMA,
            platform="windows-x64",
            purpose="internal",
            houdini="22.0.466",
            files=[dict(path=name, bytes=len(content), sha256=helper.digest(content))],
        )
        manifest["package_id"] = helper.digest(helper.canonical(manifest))
        archive = self.root / "reference.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(name, content)
            package.writestr(helper.MANIFEST, helper.canonical(manifest))
        output = self.root / "baseline.json"
        result = source.extract_baseline(archive, output, "22.0.466")
        self.assertEqual(output.read_bytes(), content)
        self.assertEqual(result["package_id"], manifest["package_id"])
        with self.assertRaises(RuntimeError):
            source.extract_baseline(archive, self.root / "wrong.json", "22.0.429")
        self.assertFalse((self.root / "wrong.json").exists())
        broken = self.root / "corrupt-reference.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(broken, "w") as package:
            for item in original.namelist():
                package.writestr(item, b"corrupt" if item == name else original.read(item))
        with self.assertRaises(RuntimeError):
            source.extract_baseline(broken, self.root / "corrupt.json", "22.0.466")
        self.assertFalse((self.root / "corrupt.json").exists())

    def test_locked_inventory_entries_are_source_verified(self):
        inventory = module("patch-inventory").inventory(ROOT)
        lock = json.loads((ROOT / "source.lock.json").read_text())
        self.assertEqual(inventory["patch_count"], len(lock["cycles"]["patches"]))
        self.assertTrue(
            all(
                item["regression"]["assert_statements"] + item["regression"]["unittest_assert_calls"] > 0
                for item in inventory["patches"]
                if item["regression"]["related_fixture"]
            )
        )


if __name__ == "__main__":
    unittest.main()
