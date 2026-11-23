# SPDX-License-Identifier: Apache-2.0
"""Read the patched checkout identity shared by build and release gates."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile


def engine_route(checkout, engine, env=None):
    """Name how an engine checkout matches the cycles section of source.lock.json.

    - "private": HEAD is cycles.authoring_tip (a copy of the authoring fork).
    - "public": HEAD's Git tree is cycles.authoring_tree. The public route
      (upstream plus patches/, applied with git am) recreates the commits, so
      their ids differ, but the committed files are exactly the locked ones.
    - None: neither, for example a modified or different engine.
    Uncommitted edits are not seen here; callers also require a clean checkout.
    """

    def revision(spec):
        result = subprocess.run(
            ["git", "-C", str(Path(checkout)), "rev-parse", "--verify", "--quiet", spec],
            env=env,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    tip, tree = engine.get("authoring_tip"), engine.get("authoring_tree")
    if tip and revision("HEAD") == tip:
        return "private"
    if tree and revision("HEAD^{tree}") == tree:
        return "public"
    return None


def engine_identity(checkout, engine, env=None):
    """Return the engine's route, HEAD, tree and the HEAD a build may record.

    expected_head is cycles.authoring_tip on the private route, the checkout's
    own HEAD on the public route (its tree was checked), and cycles.commit for a
    lock without a fork tip ("upstream"). Raises when a fork is locked but the
    checkout matches neither route.
    """
    route = engine_route(checkout, engine, env)

    def revision(spec):
        return subprocess.check_output(
            ["git", "-C", str(Path(checkout)), "rev-parse", spec], env=env, text=True
        ).strip()

    head, tree = revision("HEAD"), revision("HEAD^{tree}")
    if route is None and engine.get("authoring_tip"):
        raise RuntimeError(
            "Engine checkout matches neither route in source.lock.json: HEAD "
            + head
            + " is not cycles.authoring_tip (private route) and its Git tree "
            + tree
            + " is not cycles.authoring_tree (public route)"
        )
    if route is None:
        route, expected = "upstream", engine["commit"]
    else:
        expected = head if route == "public" else engine["authoring_tip"]
    return dict(route=route, head=head, tree=tree, expected_head=expected)


def fingerprint(checkout, env=None):
    checkout = Path(checkout).resolve()

    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(checkout), *arguments], env=env)

    difference = git("diff", "--binary", "HEAD", "--")
    status = git("status", "--porcelain=v1", "-z")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    files = []
    for name in sorted(n for n in untracked.decode("utf-8").split(chr(0)) if n):
        path = (checkout / name).resolve()
        path.relative_to(checkout)
        files.append(dict(path=name, bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return dict(
        dirty=bool(status),
        tracked_diff_sha256=hashlib.sha256(difference).hexdigest(),
        status_sha256=hashlib.sha256(status).hexdigest(),
        untracked_files=files,
    )


def legacy_fingerprint(checkout, upstream, expected, layout=None):
    """Reproduce a pre-fork baseline using only a disposable index/repository.

    The baseline's untracked list defines untracked additions. Other additions
    are represented as intent-to-add, matching the historical recorder's layout.
    An arbitrary staging layout is deliberately unsupported: exact fingerprint
    equality remains the acceptance condition. Real HEAD/index/files never move.
    """
    checkout = Path(checkout).resolve()

    def live_git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args])

    objects = Path(live_git("rev-parse", "--git-path", "objects").decode().strip())
    if not objects.is_absolute():
        objects = checkout / objects
    with tempfile.TemporaryDirectory(prefix="cycles-legacy-fingerprint-") as temporary:
        worktree = checkout
        if layout is not None:
            # Three historical source files used CRLF although the accepted
            # authoring commits intentionally store LF. Recreate only the
            # documented, hashed byte layout, in an isolated archive extraction.
            layout = json.loads(Path(layout).read_text(encoding="utf-8"))
            if layout["upstream"] != upstream or layout["baseline_fingerprint"] != expected:
                raise RuntimeError("Legacy byte layout does not describe this exact baseline and fork")
            live_git("merge-base", "--is-ancestor", layout["fork_tip"], "HEAD")
            worktree = Path(temporary) / "worktree"
            worktree.mkdir()
            with tarfile.open(
                fileobj=io.BytesIO(live_git("-c", "core.autocrlf=false", "archive", layout["fork_tip"]))
            ) as archive:
                for member in archive.getmembers():
                    (worktree / member.name).resolve().relative_to(worktree.resolve())
                    if member.issym() or member.islnk():
                        raise RuntimeError("Unsupported source archive link")
                archive.extractall(worktree, filter="data")
            for record in layout["crlf_files"]:
                target = (worktree / record["path"]).resolve()
                target.relative_to(worktree.resolve())
                data = target.read_bytes()
                if hashlib.sha256(data).hexdigest() != record["fork_sha256"]:
                    raise RuntimeError("Fork bytes differ from historical line-ending record")
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                if hashlib.sha256(data).hexdigest() != record["legacy_sha256"]:
                    raise RuntimeError("Historical source bytes cannot be reproduced")
                target.write_bytes(data)
        git_dir = Path(temporary) / "git"
        subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True)
        env = os.environ.copy()
        env.update(
            GIT_DIR=str(git_dir),
            GIT_WORK_TREE=str(worktree),
            GIT_INDEX_FILE=str(Path(temporary) / "index"),
            GIT_ALTERNATE_OBJECT_DIRECTORIES=str(objects.resolve()),
        )

        def isolated_git(*args):
            return subprocess.check_output(
                ["git", "-c", "core.autocrlf=false", "-c", "core.filemode=false", "-C", str(worktree), *args], env=env
            )

        isolated_git("config", "core.autocrlf", "false")
        isolated_git("config", "core.safecrlf", "false")
        isolated_git("config", "core.filemode", "false")
        isolated_git("update-ref", "--no-deref", "HEAD", upstream)
        isolated_git("read-tree", upstream)
        additions = set(
            live_git("diff", "--name-only", "--diff-filter=A", "-z", upstream, "HEAD").decode().split("\0")
        ) - {""}
        untracked = {item["path"] for item in expected["untracked_files"]}
        intent = sorted(additions - untracked)
        if intent:
            isolated_git("add", "--intent-to-add", "--", *intent)
        actual = fingerprint(worktree, env)
    if actual != expected:
        raise RuntimeError("Legacy source fingerprint is not exactly reproducible from the current fork")
    return actual
