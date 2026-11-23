# SPDX-License-Identifier: Apache-2.0
"""Explicit integration release inputs, restorable snapshot and preservation gates."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import zipfile
import runpy

ENGINE_SOURCE = runpy.run_path(str(Path(__file__).with_name("engine_source.py")))
engine_fingerprint = ENGINE_SOURCE["fingerprint"]
# Release snapshots and packages keep the exact authoring commits, so they need
# the private engine route. A public-route checkout (upstream plus patches/,
# same files, new commit ids) is for development builds and tests only.
PRIVATE_ROUTE_ONLY = (
    "Release steps support only the private engine route: third_party/cycles at cycles.authoring_tip "
    "(scripts/bootstrap.ps1 -EngineSource Private). This {0} comes from the public route "
    "(upstream Cycles plus patches/), which is for development builds and tests."
)
legacy_fingerprint = runpy.run_path(str(Path(__file__).with_name("engine_source.py")))["legacy_fingerprint"]

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = (
    "blender",
    "cmake",
    "include",
    "src",
    "houdini",
    "patches",
    "scripts",
    "packaging",
    "tests",
    "docs",
    "launchers",
    ".githooks",
    ".github",
)
FILES = (
    "START HERE - Build Cycles for Houdini.cmd",
    "build-identity.json",
    "CMakeLists.txt",
    "source.lock.json",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "CMakePresets.json",
    ".clang-format",
    "pyproject.toml",
    "pytest.ini",
    ".gitignore",
    ".gitattributes",
    "examples/sop-fx-courtyard/build_scene.py",
    "examples/sop-fx-courtyard/native_geometry.py",
    "examples/sop-fx-courtyard/adaptive_scene.py",
    "examples/sop-fx-courtyard/open_scene.py",
)


# These are recovery inputs even if someone accidentally removes a directory
# from the archive inventory. Blender staging and packaging need this entry
# point and its implementation, and the restored sources keep the builder.
RESTORE_INPUTS = frozenset(
    (
        "START HERE - Build Cycles for Houdini.cmd",
        "build-identity.json",
        "blender/LICENSE",
        "blender/worker_main.py",
        "blender/export_main.py",
        "blender/asset_prefs_main.py",
        "blender/cycles_blender/worker_server.py",
        "blender/cycles_blender/blend_export.py",
    )
)


def require_restore_inputs(inventory):
    missing = RESTORE_INPUTS - {item["path"] for item in inventory}
    if missing:
        raise RuntimeError("Snapshot lacks required recovery inputs: " + ", ".join(sorted(missing)))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def records(root):
    paths = {root / name for name in FILES if (root / name).is_file()}
    for name in DIRECTORIES:
        paths.update(
            path
            for path in (root / name).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")
        )
    return [dict(path=path.relative_to(root).as_posix(), sha256=digest(path.read_bytes())) for path in sorted(paths)]


def snapshot(root, output):
    inventory = records(root)
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
    # Include the committed fork and all ancestors down to the shallow upstream
    # boundary. Archiving only upstream loses the accepted authoring commit IDs.
    engine_head = lock["cycles"].get("authoring_tip", lock["cycles"]["commit"])
    if "authoring_tip" in lock["cycles"]:
        if ENGINE_SOURCE["engine_route"](root / "third_party/cycles", lock["cycles"]) == "public":
            raise RuntimeError(PRIVATE_ROUTE_ONLY.format("engine checkout"))
    require_restore_inputs(inventory)
    if "authoring_tip" in lock["cycles"]:
        runpy.run_path(str(Path(__file__).with_name("engine-fork.py")))["verify"](root, root / "third_party/cycles")
    engine = subprocess.check_output(
        ["git", "-C", str(root / "third_party/cycles"), "pack-objects", "--stdout", "--revs"],
        input=(engine_head + chr(10)).encode(),
    )
    shallow_path = subprocess.check_output(
        ["git", "-C", str(root / "third_party/cycles"), "rev-parse", "--git-path", "shallow"], text=True
    ).strip()
    shallow_file = root / "third_party/cycles" / shallow_path
    shallow = shallow_file.read_bytes() if shallow_file.exists() else b""
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in inventory:
            content = (root / item["path"]).read_bytes()
            if digest(content) != item["sha256"]:
                raise RuntimeError("Source changed during snapshot: " + item["path"])
            archive.writestr("integration/" + item["path"], content)
        archive.writestr("pinned-engine.pack", engine)
        archive.writestr("pinned-engine.shallow", shallow)
        archive.writestr(
            "snapshot.json",
            json.dumps(
                dict(
                    schema_version=3 if "authoring_tip" in lock["cycles"] else 2,
                    integration_source=inventory,
                    engine_commit=lock["cycles"]["commit"],
                    engine_head=engine_head,
                    engine_fingerprint=engine_fingerprint(root / "third_party/cycles"),
                    engine_archive_sha256=digest(engine),
                    engine_shallow_sha256=digest(shallow),
                    restore="Extract integration/ as source root. Use scripts/source-history.py restore-engine-snapshot --archive <snapshot.zip> --output <NEW engine path> for version 3. It restores the exact fork and verifies independent patch replay. Version 2: unpack pinned-engine.pack, restore pinned-engine.shallow, checkout engine_commit and apply locked patches in order. Fetch locked dependencies, DLSS and OptiX revisions; use the qualified Houdini SDK.",
                ),
                indent=2,
            ),
        )
    return dict(result="passed", snapshot=str(output), sha256=digest(output.read_bytes()), files=len(inventory))


def check_snapshot(root, archive):
    with zipfile.ZipFile(archive) as source:
        metadata = json.loads(source.read("snapshot.json"))
        if metadata.get("schema_version") not in (2, 3) or "engine_fingerprint" not in metadata:
            raise RuntimeError(
                "Source certification requires a version 2 snapshot or newer with a patched-engine fingerprint"
            )
        lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
        if lock["cycles"].get("authoring_tip") and (
            metadata.get("schema_version") != 3 or metadata.get("engine_head") != lock["cycles"]["authoring_tip"]
        ):
            raise RuntimeError("Snapshot must preserve the locked production fork commit")
        for name, key in [
            ("pinned-engine.pack", "engine_archive_sha256"),
            ("pinned-engine.shallow", "engine_shallow_sha256"),
        ]:
            if digest(source.read(name)) != metadata[key]:
                raise RuntimeError("Corrupt engine snapshot: " + name)
        frozen = metadata["integration_source"]
        require_restore_inputs(frozen)
        for item in frozen:
            if digest(source.read("integration/" + item["path"])) != item["sha256"]:
                raise RuntimeError("Corrupt snapshot member: " + item["path"])
    if frozen != records(root):
        raise RuntimeError("Release inputs changed since source snapshot; create a new release run")
    if metadata.get("engine_fingerprint") != engine_fingerprint(root / "third_party/cycles"):
        raise RuntimeError("Patched engine changed since accepted source snapshot")
    return dict(result="passed", files=len(frozen))


def matches_engine_prefix(engine, upstream, tip, baseline_tip, baseline_count, current_count):
    if baseline_tip == tip:
        return baseline_count == current_count
    history = subprocess.check_output(
        ["git", "-C", str(engine), "rev-list", "--reverse", upstream + ".." + tip], text=True
    ).splitlines()
    return (
        len(history) == current_count
        and 0 < baseline_count <= len(history)
        and history[baseline_count - 1] == baseline_tip
    )


def kernel_payload_preserved(engine, previous, current, extension_base):
    if current["kernels"] == previous["kernels"]:
        return True
    if not extension_base:
        return False
    if {row["name"] for row in current["kernels"]} != {row["name"] for row in previous["kernels"]}:
        return False
    changes = subprocess.check_output(
        [
            "git",
            "-C",
            str(engine),
            "diff",
            "--name-only",
            extension_base,
            current["actual_revisions"]["cycles"],
            "--",
            "src/kernel",
            "src/util",
        ],
        text=True,
    ).splitlines()
    return bool(changes)


def approved_rebase(root, baseline, approval):
    """A reviewed source transition permits recompilation, never an arbitrary baseline reset."""
    if not baseline:
        raise RuntimeError("Rebase approval requires an exact previous build manifest")
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))["cycles"]
    expected_path = lock.get("release_rebase_approval")
    if not expected_path or (root / expected_path).resolve() != approval.resolve():
        raise RuntimeError("Rebase approval must be the decision receipt named in the source lock")
    engine = root / "third_party/cycles"
    actual_tip = subprocess.check_output(["git", "-C", str(engine), "rev-parse", "HEAD"], text=True).strip()
    fingerprint = engine_fingerprint(engine)
    if actual_tip != lock["authoring_tip"] or fingerprint["dirty"]:
        raise RuntimeError(
            "Release rebase approval requires the exact clean locked fork (private engine route, HEAD at cycles.authoring_tip)"
        )
    record = json.loads(approval.read_text(encoding="utf-8"))
    decision = record.get("decision", "")
    if not re.fullmatch(r"docs/decisions/\d{4}-[\w-]+\.md", decision):
        raise RuntimeError("Rebase approval requires an explicit review decision")
    expected = dict(
        schema_version=1,
        result="approved",
        baseline_manifest_sha256=digest(baseline.read_bytes()),
        upstream=lock["commit"],
        authoring_tip=lock["authoring_tip"],
        applied_patches=[dict(path=name, sha256=lock["patch_sha256"][name]) for name in lock["patches"]],
        engine_fingerprint=fingerprint,
        decision=decision,
        decision_sha256=digest((root / decision).read_bytes()),
    )
    if record != expected:
        raise RuntimeError("Expired or incomplete release rebase approval; full source review required")
    # This also checks all eight base/hash-bound integrator grants.
    runpy.run_path(str(Path(__file__).with_name("patch-triage.py")))["validate"](root)
    return dict(path=expected_path, sha256=digest(approval.read_bytes()), decision=decision)


def preservation(build, baseline, package, frozen=None, rebase_approval=None):
    current = json.loads((build / "build-manifest.json").read_text(encoding="utf-8-sig"))
    if current.get("engine_route") == "public":
        raise RuntimeError(PRIVATE_ROUTE_ONLY.format("build"))
    lock = json.loads((ROOT / "source.lock.json").read_text(encoding="utf-8-sig"))
    rebase = approved_rebase(ROOT, baseline, rebase_approval) if rebase_approval else None
    expected_patches = [
        dict(path=name, sha256=digest((ROOT / name).read_bytes())) for name in lock["cycles"]["patches"]
    ]
    expected_kernels = set()
    enabled = lambda key: current["features"].get(key) in ("ON", "TRUE", "1")
    if enabled("WITH_CYCLES_DEVICE_CUDA"):
        for arch in lock["certification_candidate"]["cuda_architectures"]:
            extension = "ptx" if arch.startswith("compute_") else "cubin"
            expected_kernels.add(f"kernel_{arch}.{extension}.zst")
    if enabled("WITH_CYCLES_DEVICE_OPTIX"):
        expected_kernels.update(
            ("kernel_optix.ptx.zst", "kernel_optix_mnee.ptx.zst", "kernel_optix_shader_raytrace.ptx.zst")
        )
    checks = dict(
        locked_patches=current["applied_patches"] == expected_patches,
        locked_engine=current["actual_revisions"]["cycles"]
        == lock["cycles"].get("authoring_tip", lock["cycles"]["commit"]),
        configured_kernels={item["name"] for item in current["kernels"]} == expected_kernels,
        qualified_sdk=current["host_sdk"]["houdini"] == lock["certification_candidate"]["houdini"],
    )
    checks["built_engine_matches_checkout"] = current["source_fingerprint"]["cycles"] == engine_fingerprint(
        ROOT / "third_party/cycles"
    )
    production = (
        "CH_IMAGE_IRIS",
        "WITH_DLSS",
        "WITH_CYCLES_DEVICE_CUDA",
        "WITH_CYCLES_DEVICE_OPTIX",
        "WITH_CYCLES_OPENIMAGEDENOISE",
        "WITH_CYCLES_OPENVDB",
        "WITH_CYCLES_NANOVDB",
        "WITH_CYCLES_OPENSUBDIV",
    )
    checks["production_features"] = all(enabled(key) for key in production)
    if baseline and rebase:
        previous = json.loads(baseline.read_text(encoding="utf-8-sig"))
        checks.update({key: current[key] == previous[key] for key in ("host_sdk", "features")})
        checks["rebase_dependency_revisions"] = {
            key: value for key, value in current["actual_revisions"].items() if key != "cycles"
        } == {key: value for key, value in previous["actual_revisions"].items() if key != "cycles"}
        checks["rebase_kernel_architectures"] = {row["name"] for row in current["kernels"]} == {
            row["name"] for row in previous["kernels"]
        }
        checks["reviewed_rebase"] = True
    elif baseline:
        previous = json.loads(baseline.read_text(encoding="utf-8-sig"))
        checks.update({key: current[key] == previous[key] for key in ("host_sdk", "features")})
        old_patches = previous["applied_patches"]
        checks["applied_patches"] = expected_patches[: len(old_patches)] == old_patches
        # A release can extend the locked stack. Tie the complete old prefix
        # to its exact engine commit before accepting any additional patches.
        old_tip = previous["actual_revisions"]["cycles"]
        if old_tip == lock["cycles"]["commit"] and lock["cycles"].get("authoring_tip"):
            old_tip = json.loads((ROOT / "patches/legacy-source-layout.json").read_text())["fork_tip"]
        tip = current["actual_revisions"]["cycles"]
        checks["baseline_engine_prefix"] = matches_engine_prefix(
            ROOT / "third_party/cycles", lock["cycles"]["commit"], tip, old_tip, len(old_patches), len(expected_patches)
        )
        # Recompiled kernels are expected when a verified locked successor edits
        # kernel inputs. Existing patches, target architectures and feature flags
        # are still checked against the baseline, and the tested package is frozen.
        extension_base = (
            old_tip
            if (
                checks["applied_patches"]
                and checks["baseline_engine_prefix"]
                and len(expected_patches) > len(old_patches)
                and tip != old_tip
            )
            else None
        )
        checks["kernels"] = kernel_payload_preserved(ROOT / "third_party/cycles", previous, current, extension_base)
        if previous["actual_revisions"]["cycles"] == lock["cycles"]["commit"] and current["actual_revisions"][
            "cycles"
        ] == lock["cycles"].get("authoring_tip"):
            # Retain the original baseline hashes exactly. Storage changed from
            # upstream+dirty source to a clean fork; rebuild that old view in a
            # disposable Git index and compare every historical fingerprint field.
            checks["engine_source"] = (
                legacy_fingerprint(
                    ROOT / "third_party/cycles",
                    lock["cycles"]["commit"],
                    previous["source_fingerprint"]["cycles"],
                    ROOT / "patches/legacy-source-layout.json",
                )
                == previous["source_fingerprint"]["cycles"]
            )
        else:
            checks["engine_source"] = (
                current["source_fingerprint"]["cycles"] == previous["source_fingerprint"]["cycles"]
            )
    checks["image_iris"] = current["features"]["CH_IMAGE_IRIS"] in ("ON", "TRUE", "1")
    for key in ("CH_BDPT_PROTOTYPE", "WITH_CYCLES_HYDRA_RENDER_DELEGATE", "WITH_CYCLES_USD"):
        checks[key + "_disabled"] = current["features"].get(key) in ("OFF", "FALSE", "0", "")
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
        if rebase:
            checks["packaged_rebase_approval"] = (
                archive.read("provenance/" + rebase["path"]) == rebase_approval.read_bytes()
            )
        if frozen:
            frozen_bytes = frozen.read_bytes()
            packaged = archive.read("provenance/build-manifest.json")
            if "public_payload_sha256" in manifest:
                helper = load_release_package()
                verified = helper._verify_archive(archive)
                matches = helper.matches_build_provenance(verified, packaged, frozen_bytes)
            else:
                matches = packaged == frozen_bytes
            checks["exact_tested_manifest"] = matches and frozen_bytes == (build / "build-manifest.json").read_bytes()
        checks["target"] = manifest["houdini"] == current["host_sdk"]["houdini"]
        forbidden = [
            name
            for name in archive.namelist()
            if "neural" in name.lower() or name.lower().endswith((".ngv", ".pt", ".npz")) or "/usd_plugins/" in name
        ]
        checks["production_payload"] = not forbidden
    if not all(checks.values()):
        raise RuntimeError("Preservation failed: " + json.dumps(checks))
    return dict(
        result="passed",
        checks=checks,
        reference_comparison=bool(baseline),
        reference_mode="approved-rebase" if rebase else ("exact-prefix" if baseline else "current-source"),
        rebase_approval=rebase,
        package_id=manifest["package_id"],
        runtime_fingerprint=manifest["runtime_fingerprint"],
    )


def load_release_package():
    spec = importlib.util.spec_from_file_location("release_reference_package", ROOT / "scripts/release-package.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def extract_baseline(package, output, houdini):
    helper = load_release_package()
    archive_sha256 = helper.file_hash(package)
    manifest = helper.verify_archive(package, archive_sha256)
    if manifest["houdini"] != houdini:
        raise RuntimeError("Rollback package target differs from exact release target")
    with zipfile.ZipFile(package) as archive:
        content = archive.read("provenance/build-manifest.json")
    if helper.file_hash(package) != archive_sha256:
        raise RuntimeError("Rollback package changed while reading verified provenance")
    baseline = json.loads(content.decode("utf-8-sig"))
    if baseline["host_sdk"]["houdini"] != houdini:
        raise RuntimeError("Rollback provenance SDK differs from package target")
    output.write_bytes(content)
    return dict(
        result="passed",
        package=str(package),
        package_sha256=archive_sha256,
        package_id=manifest["package_id"],
        houdini=houdini,
        baseline_manifest=str(output),
        baseline_sha256=digest(content),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("records", "snapshot", "check-snapshot", "preservation", "baseline"))
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--frozen-manifest", type=Path)
    parser.add_argument("--rebase-approval", type=Path)
    parser.add_argument("--houdini")
    args = parser.parse_args()
    if args.command == "records":
        result = records(args.source_root)
    elif args.command == "snapshot":
        result = snapshot(args.source_root, args.output)
    elif args.command == "check-snapshot":
        result = check_snapshot(args.source_root, args.snapshot)
    elif args.command == "baseline":
        result = extract_baseline(args.package, args.baseline, args.houdini)
    else:
        result = preservation(args.build_root, args.baseline, args.package, args.frozen_manifest, args.rebase_approval)
    if args.output and args.command != "snapshot":
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
