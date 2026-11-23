# SPDX-License-Identifier: Apache-2.0
"""Verify the engine checkout or materialize it at a NEW destination; never reset live source.

Two routes give the same engine source:
- public: upstream Cycles at cycles.commit plus patches/ in locked order (materialize-public);
  the result's Git tree must equal cycles.authoring_tree.
- private: a copy of the authoring fork whose HEAD is cycles.authoring_tip (materialize).
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
METADATA = runpy.run_path(str(Path(__file__).with_name("engine_commit_metadata.py")))
ENGINE_SOURCE = runpy.run_path(str(Path(__file__).with_name("engine_source.py")))


PUBLIC_BRANCH = "houdini/public-replay"
FETCH_ATTEMPTS = 4
FETCH_FIRST_PAUSE = 5.0
FETCH_NOT_ANSWERED = (
    "The download from {host} did not answer, your internet connection or the server may be busy: "
    "double-click the file again (nothing is left half-installed)."
)
PATCH_AUTHOR = re.compile(rb"^From: (.+?) <([^<>]*)>$", re.MULTILINE)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def lock_engine(root):
    return json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))["cycles"]


def locked_patches(root, engine):
    """Read patches/ in locked order and check each against cycles.patch_sha256.

    The recorded hashes are of the LF bytes Git stores. A Windows checkout with
    core.autocrlf=true has CRLF copies of the same patches, so compare LF bytes.
    """
    result = []
    for name in engine["patches"]:
        data = (root / name).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != engine["patch_sha256"].get(name):
            raise RuntimeError("Patch file differs from source.lock.json (cycles.patch_sha256): " + name)
        result.append((name, data))
    return result


def route(root, repo):
    """Name the route an engine checkout came from: private, public, or None (unknown)."""
    return ENGINE_SOURCE["engine_route"](repo, lock_engine(root))


def verify(root, repo, output=None):
    engine = lock_engine(root)
    tip = engine.get("authoring_tip")
    if not tip:
        raise RuntimeError("Production engine requires cycles.authoring_tip in source.lock.json")
    kind = route(root, repo)
    if kind is None:
        raise RuntimeError(
            "Production engine HEAD must equal cycles.authoring_tip (private route), or its Git tree must equal cycles.authoring_tree (public route). Preserve the current checkout, then use engine-fork.py materialize or materialize-public at a NEW path; never reset live source."
        )
    if git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        raise RuntimeError("Production fork must be clean, including submodules and untracked files")
    if kind == "private":
        commits = len(METADATA["locked_metadata"](root, repo)["commits"])
        tree = engine.get("authoring_tree")
        if tree and git(repo, "rev-parse", tip + "^{tree}").decode().strip() != tree:
            # Warn only: the private route must keep working while the lock is being updated.
            print("warning: cycles.authoring_tree is stale; record: engine-fork.py tree", file=sys.stderr)
    else:
        locked_patches(root, engine)
        git(repo, "merge-base", "--is-ancestor", engine["commit"], "HEAD")
        commits = int(git(repo, "rev-list", "--count", engine["commit"] + "..HEAD").decode())
        if commits != len(engine["patches"]):
            raise RuntimeError("Public engine replay must have one commit per locked patch on top of cycles.commit")
    replay = runpy.run_path(str(Path(__file__).with_name("verify-upstream-patches.py")))
    output = output or root / "artifacts/engine-fork" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    report = replay["run"](root, output, repo)
    return dict(
        result="passed",
        route=kind,
        upstream=engine["commit"],
        tip=git(repo, "rev-parse", "HEAD").decode().strip(),
        tree=git(repo, "rev-parse", "HEAD^{tree}").decode().strip(),
        commits=commits,
        replay_report=str(output / "report.json"),
        compared_files=len(report["files"]),
    )


def materialize_public(root, destination, output=None):
    """Download upstream Cycles at cycles.commit and apply patches/ in locked order.

    Works in DESTINATION.incomplete and renames it to DESTINATION only after the
    resulting Git tree equals cycles.authoring_tree, so a failed run never leaves
    a checkout that looks finished. Needs network access to cycles.url.
    """
    if destination.exists():
        raise RuntimeError("Destination exists; refusing to change any existing checkout")
    engine = lock_engine(root)
    expected_tree = engine.get("authoring_tree")
    if not expected_tree:
        raise RuntimeError("source.lock.json has no cycles.authoring_tree to check the replayed engine against")
    patches = locked_patches(root, engine)
    work = destination.with_name(destination.name + ".incomplete")
    if work.exists():
        raise RuntimeError(f"Delete the unfinished folder from an earlier attempt first: {work}")
    work.mkdir(parents=True)
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)

    def run(*args, data=None, extra=None):
        done = subprocess.run(
            ["git", "-C", str(work), *args], input=data, env={**env, **(extra or {})}, capture_output=True
        )
        if done.returncode:
            raise RuntimeError(f"git {' '.join(args[:4])} failed: " + done.stderr.decode(errors="replace").strip())
        return done.stdout

    run("init", "-q")
    run("config", "core.autocrlf", "false")
    run("config", "rerere.enabled", "true")
    run("remote", "add", "origin", engine["url"])
    print(f"Fetching {engine['url']} at {engine['commit']}", file=sys.stderr, flush=True)
    # A busy connection or server: the fetch is tried a few times with a short growing pause; if it never
    # answers, the unfinished folder is removed again, so the next run starts clean.
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            run("fetch", "--depth", "1", "--no-tags", "origin", engine["commit"])
            break
        except RuntimeError as error:
            if attempt == FETCH_ATTEMPTS:
                shutil.rmtree(work, ignore_errors=True)
                print(f"   the last try did not finish either ({error})", file=sys.stderr, flush=True)
                raise RuntimeError(FETCH_NOT_ANSWERED.format(host=engine["url"].split("/")[2])) from None
            pause = FETCH_FIRST_PAUSE * 2 ** (attempt - 1)
            print(f"   try {attempt} of {FETCH_ATTEMPTS} did not finish; trying again in {pause:.0f} s", file=sys.stderr, flush=True)
            time.sleep(pause)
    run("checkout", "-q", "-b", PUBLIC_BRANCH, engine["commit"])
    print(f"Applying {len(patches)} patches from patches/", file=sys.stderr, flush=True)
    for name, data in patches:
        author = PATCH_AUTHOR.search(data.split(b"\n\n", 1)[0])
        if author is None:
            raise RuntimeError("Patch has no From: author line: " + name)
        committer = dict(
            GIT_COMMITTER_NAME=author.group(1).decode("utf-8"), GIT_COMMITTER_EMAIL=author.group(2).decode("utf-8")
        )
        try:
            run(
                "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "apply.whitespace=nowarn",
                "am", "-q", "--no-3way", "--whitespace=nowarn", "--committer-date-is-author-date",
                data=data, extra=committer,
            )  # fmt: skip
        except RuntimeError as error:
            subprocess.run(["git", "-C", str(work), "am", "--abort"], env=env, capture_output=True)
            raise RuntimeError(f"{name} does not apply to upstream {engine['commit']}: {error}") from None
    tree = run("rev-parse", "HEAD^{tree}").decode().strip()
    if tree != expected_tree:
        raise RuntimeError(
            f"Replayed engine tree {tree} differs from cycles.authoring_tree {expected_tree}; left in {work}"
        )
    work.rename(destination)
    return verify(root, destination, output)


def materialize(root, source, destination, output=None):
    if destination.exists():
        raise RuntimeError("Destination exists; refusing to change any existing checkout")
    # Verify before copying, then verify the independent materialized repository.
    verify(root, source, output)
    tip = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))["cycles"]["authoring_tip"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(
        destination.parent,
        "clone",
        "--no-hardlinks",
        "--no-checkout",
        str(source.resolve()),
        str(destination.resolve()),
    )
    git(destination, "config", "core.autocrlf", "false")
    git(destination, "config", "rerere.enabled", "true")
    git(destination, "checkout", "-b", "houdini/production", tip)
    METADATA["restore_notes"](root, destination)
    return verify(root, destination)


def adopt(root, source, destination, output):
    """Coordinated legacy conversion, with exact local rollback; never move libraries.

    Run only after source-writing/build jobs are quiescent and a full source
    recovery archive has been captured. This writes three documented line-ending
    conversions plus Git metadata. Existing dependency checkouts remain in place.
    """
    if output.exists():
        raise RuntimeError("Migration evidence destination must be new")
    output.mkdir(parents=True)
    verify(root, source, output / "author-replay")
    identity = runpy.run_path(str(Path(__file__).with_name("engine_source.py")))
    layout_path = root / "patches/legacy-source-layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    before = identity["fingerprint"](destination)
    old_head = git(destination, "rev-parse", "HEAD").decode().strip()
    if old_head != layout["upstream"] or before != layout["baseline_fingerprint"]:
        raise RuntimeError("Live source differs from the exact preserved legacy baseline; no files changed")
    identity["legacy_fingerprint"](source, old_head, before, layout_path)
    old_ref = subprocess.run(["git", "-C", str(destination), "symbolic-ref", "-q", "HEAD"], capture_output=True)
    if old_ref.returncode not in (0, 1):
        old_ref.check_returncode()
    old_ref = old_ref.stdout.decode().strip() or None
    index = Path(git(destination, "rev-parse", "--git-path", "index").decode().strip())
    if not index.is_absolute():
        index = destination / index
    saved_index = index.read_bytes()
    (output / "legacy.index").write_bytes(saved_index)
    saved_files = {}
    for record in layout["crlf_files"]:
        name = record["path"]
        target = (destination / name).resolve()
        target.relative_to(destination.resolve())
        data = target.read_bytes()
        wanted = (source / name).read_bytes()
        if (
            hashlib.sha256(data).hexdigest() != record["legacy_sha256"]
            or hashlib.sha256(wanted).hexdigest() != record["fork_sha256"]
            or data.replace(b"\r\n", b"\n") != wanted
        ):
            raise RuntimeError("Source bytes changed during migration preflight: " + name)
        saved_files[name] = data
        backup = output / "legacy-files" / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(data)
    report = dict(
        result="prepared",
        old_head=old_head,
        old_ref=old_ref,
        new_head=layout["fork_tip"],
        baseline_fingerprint=before,
        normalized_files=list(saved_files),
        scope="Only documented source line endings, index and HEAD; no dependency or binary moves",
    )

    def save():
        (output / "migration.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    branch = "refs/heads/houdini/production"
    if subprocess.run(["git", "-C", str(destination), "show-ref", "--verify", "--quiet", branch]).returncode == 0:
        raise RuntimeError("Production fork branch already exists; review prior migration before retrying")
    try:
        # Import objects without touching the working tree or dependency checkout.
        pack = subprocess.check_output(
            ["git", "-C", str(source), "pack-objects", "--stdout", "--revs"], input=(layout["fork_tip"] + "\n").encode()
        )
        subprocess.run(["git", "-C", str(destination), "unpack-objects", "-q"], input=pack, check=True)
        if identity["fingerprint"](destination) != before:
            raise RuntimeError("Live source changed before migration commit")
        for name in saved_files:
            (destination / name).write_bytes((source / name).read_bytes())
        git(destination, "read-tree", layout["fork_tip"])
        git(destination, "update-ref", branch, layout["fork_tip"], "0" * 40)
        git(destination, "symbolic-ref", "HEAD", branch)
        report["verification"] = verify(root, destination, output / "production-replay")
        identity["legacy_fingerprint"](destination, old_head, before, layout_path)
        report["result"] = "passed"
        report["production_fingerprint"] = identity["fingerprint"](destination)
        save()
    except BaseException:
        # Roll back exact touched bytes and index; never reset/checkout source.
        for name, data in saved_files.items():
            (destination / name).write_bytes(data)
        index.write_bytes(saved_index)
        if old_ref:
            git(destination, "symbolic-ref", "HEAD", old_ref)
        else:
            git(destination, "update-ref", "--no-deref", "HEAD", old_head)
        report["result"] = (
            "failed-restored" if identity["fingerprint"](destination) == before else "failed-rollback-mismatch"
        )
        save()
        raise
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("verify", "notes", "route", "tree", "materialize", "materialize-public", "adopt-legacy")
    )
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument(
        "--engine-root", type=Path, help="Verified authoring source for materialize; production checkout for verify"
    )
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--output", type=Path, help="New directory for independent replay evidence")
    args = parser.parse_args()
    root = args.source_root.resolve()
    repo = (args.engine_root or root / "third_party/cycles").resolve()
    if args.command == "route":
        # Print only the route name, for scripts; fail when the checkout matches neither route.
        kind = route(root, repo)
        if kind is None:
            raise SystemExit("third_party/cycles matches neither cycles.authoring_tip nor cycles.authoring_tree")
        print(kind)
        raise SystemExit(0)
    if args.command == "tree":
        # The value to record as cycles.authoring_tree after moving cycles.authoring_tip.
        print(git(repo, "rev-parse", lock_engine(root)["authoring_tip"] + "^{tree}").decode().strip())
        raise SystemExit(0)
    if args.command == "materialize-public":
        if args.destination is None:
            parser.error("materialize-public requires --destination")
        result = materialize_public(root, args.destination.resolve(), args.output)
    elif args.command in ("materialize", "adopt-legacy"):
        if args.destination is None:
            parser.error(args.command + " requires --destination")
        if args.command == "adopt-legacy":
            if args.output is None:
                parser.error("adopt-legacy requires --output for rollback evidence")
            result = adopt(root, repo, args.destination.resolve(), args.output.resolve())
        else:
            result = materialize(root, repo, args.destination.resolve(), args.output)
    elif args.command == "notes":
        if route(root, repo) == "public":
            # Notes annotate commits of the private fork; the public replay has its own commits.
            result = dict(result="passed", route="public", notes=0)
        else:
            result = dict(result="passed", notes=METADATA["restore_notes"](root, repo))
    else:
        result = verify(root, repo, args.output)
    print(json.dumps(result, indent=2))
