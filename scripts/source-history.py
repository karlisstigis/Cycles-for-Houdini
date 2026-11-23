# SPDX-License-Identifier: Apache-2.0
"""Local integration/engine history and portable, offline recovery (no build)."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import runpy
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = "release/466-28f9f480"
PRODUCTION_NOTES = "refs/notes/cycles-houdini"
ENGINE_FINGERPRINT = runpy.run_path(str(Path(__file__).with_name("engine_source.py")))["fingerprint"]


def git(repo, *args, data=None, env=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data, env=env)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def default_snapshot(root):
    name = "releases/blend-batch-466-20261108/source-snapshot.zip"
    local = root / "_work" / name
    return local if local.is_file() else root / "artifacts" / name


def safe_source_path(root, name):
    # Reject Git administrative paths, including Windows trailing-dot/space
    # spellings, before touching the recovered working directory.
    if "\\" in name or ":" in name or name.startswith("/"):
        raise RuntimeError("Unsafe recovered source path: " + name)
    parts = name.split("/")
    if any(part in ("", ".", "..") or part.rstrip(" .").casefold() == ".git" for part in parts):
        raise RuntimeError("Unsafe recovered source path: " + name)
    target = (root / name).resolve()
    target.relative_to(root.resolve())
    return target


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def configure(repo):
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.safecrlf", "false")


def head_ref(repo):
    result = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True)
    if result.returncode == 1:
        return None
    result.check_returncode()
    return result.stdout.decode().strip()


def init_integration(root, snapshot):
    """Construct the release tree in a temporary index; never checkout over root."""
    if (root / ".git").exists():
        raise RuntimeError("Integration Git already exists; refusing to replace history")
    with zipfile.ZipFile(snapshot) as archive:
        metadata = json.loads(archive.read("snapshot.json"))
        entries = metadata["integration_source"]
        with tempfile.TemporaryDirectory(dir=root / "artifacts", prefix="git-baseline-") as temp:
            staging = Path(temp)
            for item in entries:
                content = archive.read("integration/" + item["path"])
                if sha(content) != item["sha256"]:
                    raise RuntimeError("Snapshot hash mismatch: " + item["path"])
                target = (staging / item["path"]).resolve()
                target.relative_to(staging.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            git(root, "init", "-b", "main")
            configure(root)
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(staging / "baseline.index")
            git(
                root,
                "--work-tree=" + str(staging),
                "add",
                "-f",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                data=("\0".join(i["path"] for i in entries) + "\0").encode(),
                env=env,
            )
            tree = git(root, "write-tree", env=env).decode().strip()
            message = (
                "Import tested Houdini 22.0.466 package 28f9f480 source snapshot\n\n"
                f"Exact {len(entries)} integration inputs from source-snapshot.zip.\n"
                f"Snapshot-SHA256: {sha(snapshot.read_bytes())}\n"
                "Production working files and nested engine were not checked out or changed.\n"
            )
            commit = git(root, "commit-tree", tree, data=message.encode()).decode().strip()
            git(root, "update-ref", "refs/heads/main", commit)
            git(root, "tag", "-a", RELEASE_TAG, commit, "-m", message)
            git(root, "read-tree", commit)
    return dict(
        result="passed",
        commit=commit,
        tag=RELEASE_TAG,
        integration_inputs=len(entries),
        source_snapshot_sha256=sha(snapshot.read_bytes()),
    )


def restore_pack(repo, pack, shallow, commit, branch):
    git(repo.parent, "init", "-q", str(repo))
    configure(repo)
    git(repo, "unpack-objects", "-q", data=pack)
    if shallow:
        (repo / ".git/shallow").write_bytes(shallow)
    if branch is None:
        git(repo, "update-ref", "--no-deref", "HEAD", commit)
    else:
        git(repo, "update-ref", "refs/heads/" + branch, commit)
        git(repo, "symbolic-ref", "HEAD", "refs/heads/" + branch)
    # Only called on a newly created, empty recovery/authoring path.
    git(repo, "reset", "--hard", "-q", "HEAD")


def restore_engine_snapshot(root, archive_path, output):
    """Restore exact fork commit identities from a new source snapshot, offline."""
    if output.exists():
        raise RuntimeError("Engine snapshot destination exists; refusing to overwrite source")
    with zipfile.ZipFile(archive_path) as archive:
        metadata = json.loads(archive.read("snapshot.json"))
        if metadata.get("schema_version") != 3:
            raise RuntimeError("Exact fork restore requires a version 3 source snapshot")
        pack = archive.read("pinned-engine.pack")
        shallow = archive.read("pinned-engine.shallow")
        if sha(pack) != metadata["engine_archive_sha256"] or sha(shallow) != metadata["engine_shallow_sha256"]:
            raise RuntimeError("Snapshot engine archive hash mismatch")
        lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
        if (
            metadata["engine_head"] != lock["cycles"]["authoring_tip"]
            or metadata["engine_commit"] != lock["cycles"]["commit"]
        ):
            raise RuntimeError("Snapshot engine identity differs from current source lock")
    output.parent.mkdir(parents=True, exist_ok=True)
    restore_pack(output, pack, shallow, metadata["engine_head"], "houdini/production")
    git(output, "config", "rerere.enabled", "true")
    runpy.run_path(str(Path(__file__).with_name("engine_commit_metadata.py")))["restore_notes"](root, output)
    return runpy.run_path(str(Path(__file__).with_name("engine-fork.py")))["verify"](root, output)


def init_author(root, snapshot, repo, resume=False):
    if repo.exists() and not resume:
        raise RuntimeError("Authoring target exists; use a new empty path")
    repo.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(snapshot) as archive:
        metadata = json.loads(archive.read("snapshot.json"))
        pack = archive.read("pinned-engine.pack")
        shallow = archive.read("pinned-engine.shallow")
        if sha(pack) != metadata["engine_archive_sha256"] or sha(shallow) != metadata["engine_shallow_sha256"]:
            raise RuntimeError("Snapshot engine archive hash mismatch")
        lock = json.loads(archive.read("integration/source.lock.json"))
        inventory = json.loads(archive.read("integration/patches/inventory.json"))
        by_path = {item["path"]: item for item in inventory["patches"]}
        previous = []
        if repo.exists():
            if git(repo, "status", "--porcelain").strip():
                raise RuntimeError("Resume requires a clean authoring repository")
            previous = git(repo, "rev-list", "--reverse", lock["cycles"]["commit"] + "..HEAD").decode().splitlines()
            if len(previous) > len(lock["cycles"]["patches"]):
                raise RuntimeError("Authoring history is longer than the accepted stack")
        else:
            restore_pack(repo, pack, shallow, lock["cycles"]["commit"], "houdini/locked-stack")
        git(repo, "config", "rerere.enabled", "true")
        commits = []
        for number, name in enumerate(lock["cycles"]["patches"]):
            patch = archive.read("integration/" + name)
            item = by_path[name]
            if sha(patch) != item["sha256"] or patch != (root / name).read_bytes():
                raise RuntimeError("Locked patch differs from accepted snapshot: " + name)
            if number < len(previous):
                message = git(repo, "show", "-s", "--format=%B", previous[number]).decode()
                if ("Locked-Patch: " + name + "\n") not in message or (
                    "Locked-Patch-SHA256: " + sha(patch) + "\n"
                ) not in message:
                    raise RuntimeError("Resume commit metadata differs from accepted patch: " + name)
                commits.append(dict(path=name, sha256=sha(patch), commit=previous[number]))
                continue
            git(repo, "apply", "--index", "--whitespace=nowarn", "-", data=patch.replace(b"\r\n", b"\n"))
            message = (
                item["purpose"]
                + "\n\n"
                + "Locked-Patch: "
                + name
                + "\n"
                + "Locked-Patch-SHA256: "
                + item["sha256"]
                + "\n"
                + "Contract: docs/renderer-patch-contracts.md\n"
                + "Related-Fixture: "
                + (item["regression"]["related_fixture"] or "See patch inventory")
                + "\n"
                + "Qualification: Source replay only; no new runtime qualification asserted.\n"
            )
            git(repo, "commit", "-q", "-F", "-", data=message.encode())
            commits.append(dict(path=name, sha256=sha(patch), commit=git(repo, "rev-parse", "HEAD").decode().strip()))
    tip = git(repo, "rev-parse", "HEAD").decode().strip()
    git(
        repo,
        "tag",
        "-a",
        "locked/466-28f9f480",
        "-m",
        str(len(commits)) + " accepted patches; historical patch bytes retained",
    )
    report = dict(
        result="passed",
        base=lock["cycles"]["commit"],
        tip=tip,
        patches=commits,
        repository=str(repo),
        shallow=bool(shallow),
        note="Local authoring only; third_party/cycles and locked patch bytes unchanged.",
    )
    dump(repo.parent / "engine-authoring.json", report)
    return report


def compare_author(root, repo):
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
    base = lock["cycles"]["commit"]
    count = int(git(repo, "rev-list", "--count", base + "..HEAD"))
    tracked = git(repo, "ls-files", "-z").decode().split("\0")
    changed = []
    missing = []
    checkout = root / "third_party/cycles"
    for name in filter(None, tracked):
        authored = repo / name
        if not authored.is_file():
            continue  # upstream gitlinks
        actual = checkout / name
        if not actual.is_file():
            missing.append(name)
        elif authored.read_bytes().replace(b"\r\n", b"\n") != actual.read_bytes().replace(b"\r\n", b"\n"):
            changed.append(name)
    locked_tip = lock["cycles"].get("authoring_tip")
    tip = git(repo, "rev-parse", "HEAD").decode().strip()
    report = dict(
        result="passed" if not changed and not missing and count == len(lock["cycles"]["patches"]) else "failed",
        base=base,
        tip=tip,
        commits=count,
        changed=changed,
        missing=missing,
        locked_tip_matches=locked_tip is None or locked_tip == tip,
    )
    if report["result"] != "passed" or not report["locked_tip_matches"]:
        raise RuntimeError(json.dumps(report))
    return report


def recovery(root, repo, output, revision=None):
    """Capture committed integration refs and shallow engine objects, no SDK copies."""
    if output.exists():
        raise RuntimeError("Recovery output exists; choose a new file")
    if revision is None and git(root, "diff", "--name-only", "--cached").strip():
        raise RuntimeError("Commit staged changes before recovery capture")
    integration_head = git(root, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}").decode().strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="bundle-") as directory:
        integration_bundle = Path(directory) / "integration.bundle"
        git(root, "bundle", "create", str(integration_bundle), "--all")
        git(root, "bundle", "verify", str(integration_bundle))
        refs = git(repo, "for-each-ref", "--format=%(objectname) %(refname)").decode().splitlines()
        engine_head = git(repo, "rev-parse", "HEAD").decode().strip()
        engine_head_ref = head_ref(repo)
        # Detached feature work may not be reachable from any named ref.
        engine_pack = git(
            repo,
            "pack-objects",
            "--stdout",
            "--revs",
            data=("\n".join([engine_head] + [line.split()[0] for line in refs]) + "\n").encode(),
        )
        shallow = Path(git(repo, "rev-parse", "--git-path", "shallow").decode().strip())
        if not shallow.is_absolute():
            shallow = repo / shallow
        engine_shallow = shallow.read_bytes() if shallow.exists() else b""
        production = root / "third_party/cycles"
        production_identity = ENGINE_FINGERPRINT(production)
        production_base = git(production, "rev-parse", "HEAD").decode().strip()
        members = {
            "integration.bundle": integration_bundle.read_bytes(),
            "engine-authoring.pack": engine_pack,
            "engine-authoring.shallow": engine_shallow,
        }
        # Production can advance beyond the preserved companion author repo.
        # Capture its own reachable objects instead of assuming its HEAD exists
        # in the companion pack. This also preserves separate shallow boundaries.
        notes_refs = git(production, "for-each-ref", "--format=%(refname) %(objectname)", PRODUCTION_NOTES)
        notes_tip = dict(line.split() for line in notes_refs.decode().splitlines()).get(PRODUCTION_NOTES)
        production_roots = [production_base, notes_tip] if notes_tip else [production_base]
        members["production-engine.pack"] = git(
            production, "pack-objects", "--stdout", "--revs", data=("\n".join(production_roots) + "\n").encode()
        )
        production_shallow = Path(git(production, "rev-parse", "--git-path", "shallow").decode().strip())
        if not production_shallow.is_absolute():
            production_shallow = production / production_shallow
        members["production-engine.shallow"] = production_shallow.read_bytes() if production_shallow.exists() else b""
        members["production-engine.diff"] = git(production, "diff", "--binary", "HEAD", "--")
        # Intent-to-add entries are part of the historical fingerprint. A diff
        # alone restores their bytes but turns them into ordinary untracked files.
        index_path = Path(git(production, "rev-parse", "--git-path", "index").decode().strip())
        if not index_path.is_absolute():
            index_path = production / index_path
        if list(index_path.parent.glob("sharedindex.*")):
            raise RuntimeError("Split production index is unsupported; preserve it separately")
        members["production-engine.index"] = index_path.read_bytes()
        staged = git(production, "ls-files", "--stage", "-z").split(b"\0")
        objects = sorted(
            {record.split(b"\t", 1)[0].split()[1] for record in staged if record and not record.startswith(b"160000 ")}
        )
        members["production-index-objects.pack"] = git(
            production, "pack-objects", "--stdout", data=b"\n".join(objects) + b"\n"
        )
        for item in production_identity["untracked_files"]:
            name = item["path"]
            path = safe_source_path(production, name)
            content = path.read_bytes()
            if sha(content) != item["sha256"]:
                raise RuntimeError("Production engine changed during recovery capture: " + name)
            members["production-untracked/" + name] = content
        if ENGINE_FINGERPRINT(production) != production_identity:
            raise RuntimeError("Production engine changed during recovery capture")
        manifest = dict(
            schema_version=1,
            created_utc=datetime.now(timezone.utc).isoformat(),
            integration_head=integration_head,
            engine_head=engine_head,
            engine_head_ref=engine_head_ref,
            engine_refs=refs,
            integration_branch=None if revision else git(root, "symbolic-ref", "--short", "HEAD").decode().strip(),
            production_engine_base=production_base,
            production_notes_tip=notes_tip,
            production_engine_fingerprint=production_identity,
            members={name: dict(sha256=sha(data), bytes=len(data)) for name, data in members.items()},
            scope="Committed integration refs, reachable engine-authoring objects and exact production engine working-source delta; includes shallow boundary. No SDK, dependencies, binaries, runtime activation or uncommitted integration files.",
        )
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
            archive.writestr("recovery.json", json.dumps(manifest, indent=2))
    return dict(result="passed", recovery=str(output), sha256=sha(output.read_bytes()), **manifest)


def restore_recovery(archive_path, output):
    if output.exists():
        raise RuntimeError("Recovery target exists; refusing to overwrite it")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("recovery.json"))
        members = {}
        for name, record in manifest["members"].items():
            members[name] = archive.read(name)
            if sha(members[name]) != record["sha256"]:
                raise RuntimeError("Recovery member hash mismatch: " + name)
            if name.startswith("production-untracked/"):
                safe_source_path(output / "integration/third_party/cycles", name.removeprefix("production-untracked/"))
    output.mkdir(parents=True)
    bundle = output / "integration.bundle"
    bundle.write_bytes(members["integration.bundle"])
    integration = output / "integration"
    # --no-checkout ensures local core.autocrlf is set before materializing bytes.
    git(output, "clone", "--no-checkout", "-q", str(bundle), str(integration))
    configure(integration)
    # A clone normally maps secondary branches to refs/remotes/origin and omits
    # custom refs. Restore the original local names from the offline bundle.
    git(integration, "fetch", "-q", "--no-tags", "--update-head-ok", str(bundle), "+refs/*:refs/*")
    if manifest["integration_branch"]:
        git(integration, "checkout", "-q", manifest["integration_branch"])
    else:
        git(integration, "checkout", "-q", "--detach", manifest["integration_head"])
    if git(integration, "rev-parse", "HEAD").decode().strip() != manifest["integration_head"]:
        raise RuntimeError("Recovered integration HEAD mismatch")
    engine = output / "engine-authoring"
    engine_head_ref = manifest.get("engine_head_ref", "refs/heads/houdini/locked-stack")
    if engine_head_ref is not None:
        if not engine_head_ref.startswith("refs/heads/"):
            raise RuntimeError("Unsupported authoring HEAD reference")
        git(integration, "check-ref-format", engine_head_ref)
    restore_pack(
        engine,
        members["engine-authoring.pack"],
        members["engine-authoring.shallow"],
        manifest["engine_head"],
        engine_head_ref.removeprefix("refs/heads/") if engine_head_ref else None,
    )
    for line in manifest["engine_refs"]:
        oid, ref = line.split(" ", 1)
        git(engine, "update-ref", ref, oid)
    if (
        git(engine, "rev-parse", "HEAD").decode().strip() != manifest["engine_head"]
        or head_ref(engine) != engine_head_ref
    ):
        raise RuntimeError("Recovered authoring HEAD mismatch")
    production = integration / "third_party/cycles"
    production.parent.mkdir(parents=True)
    restore_pack(
        production,
        members.get("production-engine.pack", members["engine-authoring.pack"]),
        members.get("production-engine.shallow", members["engine-authoring.shallow"]),
        manifest["production_engine_base"],
        "locked",
    )
    if members["production-engine.diff"]:
        git(production, "apply", "--binary", "--whitespace=nowarn", "-", data=members["production-engine.diff"])
    for name, content in members.items():
        if name.startswith("production-untracked/"):
            target = safe_source_path(production, name.removeprefix("production-untracked/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    git(production, "unpack-objects", "-q", data=members["production-index-objects.pack"])
    (production / ".git/index").write_bytes(members["production-engine.index"])
    if ENGINE_FINGERPRINT(production) != manifest["production_engine_fingerprint"]:
        raise RuntimeError("Recovered production engine fingerprint mismatch")
    # Notes can describe commits parked by a rebase. Only old archives have to
    # reconstruct production notes from the currently locked patch set.
    if manifest.get("production_notes_tip"):
        git(production, "update-ref", PRODUCTION_NOTES, manifest["production_notes_tip"])
        git(production, "config", "--add", "notes.displayRef", PRODUCTION_NOTES)
    lock_path = integration / "source.lock.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))["cycles"]
        if lock.get("authoring_tip"):
            metadata = runpy.run_path(str(Path(__file__).with_name("engine_commit_metadata.py")))
            for repo in (engine,) if "production_notes_tip" in manifest else (engine, production):
                if git(repo, "rev-parse", "HEAD").decode().strip() == lock["authoring_tip"]:
                    metadata["restore_notes"](integration, repo)
    git(integration, "fsck", "--full")
    git(engine, "fsck", "--full")
    git(production, "fsck", "--full")
    return dict(
        result="passed",
        integration=str(integration),
        engine=str(engine),
        integration_head=manifest["integration_head"],
        engine_head=manifest["engine_head"],
        engine_head_ref=engine_head_ref,
        shallow_boundary_restored=bool(members["engine-authoring.shallow"]),
        production_engine_fingerprint_matches=True,
        note="Offline restore. Build dependencies/qualified Houdini SDK remain external.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("init-integration", "init-author", "verify-author", "recovery", "restore", "restore-engine-snapshot"),
    )
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--author-repo", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--revision", help="Capture this reachable integration commit for a verified CI run")
    parser.add_argument("--resume", action="store_true", help="Resume clean, metadata-verified authoring history")
    args = parser.parse_args()
    root = args.source_root.resolve()
    snapshot = args.snapshot or default_snapshot(root)
    repo = args.author_repo or root / "artifacts/engine-authoring/locked-repo"
    if args.command == "init-integration":
        result = init_integration(root, snapshot)
    elif args.command == "init-author":
        result = init_author(root, snapshot, repo, args.resume)
    elif args.command == "verify-author":
        result = compare_author(root, repo)
    elif args.command == "recovery":
        result = recovery(root, repo, args.output.resolve(), args.revision)
    elif args.command == "restore-engine-snapshot":
        result = restore_engine_snapshot(root, args.archive.resolve(), args.output.resolve())
    else:
        result = restore_recovery(args.archive.resolve(), args.output.resolve())
    if args.report:
        dump(args.report, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
