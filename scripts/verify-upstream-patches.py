# SPDX-License-Identifier: Apache-2.0
"""Replay the locked stack and compare all tracked engine files and extra source."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile


def apply_patch(tree, patch):
    """Apply outside any enclosing repository (the integration root is Git now)."""
    env = os.environ.copy()
    # git apply otherwise discovers the integration repository, treats this
    # scratch directory as a prefix, and can silently skip every engine path.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    env["GIT_CEILING_DIRECTORIES"] = str(tree.resolve().parent)
    content = patch.read_bytes().replace(b"\r\n", b"\n")
    for flags in (["--check"], []):
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", *flags, "-"],
            cwd=tree,
            input=content,
            env=env,
            capture_output=True,
            check=True,
        )


def run(root, output, checkout=None):
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
    engine = lock["cycles"]
    checkout = checkout or root / "third_party/cycles"
    output.mkdir(parents=True, exist_ok=False)
    tree = output / "replayed"
    tree.mkdir()
    pristine = subprocess.check_output(
        ["git", "-C", str(checkout), "-c", "core.autocrlf=false", "archive", "--format=tar", engine["commit"]]
    )
    with tarfile.open(fileobj=io.BytesIO(pristine)) as archive:
        # Reject links/escaping members, even if a future engine pin introduces them.
        for item in archive.getmembers():
            (tree / item.name).resolve().relative_to(tree.resolve())
            if item.issym() or item.islnk():
                raise RuntimeError("Unsupported engine archive link: " + item.name)
        archive.extractall(tree, filter="data")
    # Replay has always compared CRLF-normalized source. Normalize text members
    # before applying equally normalized patches too: Git archive can perform
    # checkout conversions, and a valid upstream blob can itself contain CRLF.
    # Binary members remain byte-for-byte intact.
    for path in tree.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if b"\0" not in data and b"\r\n" in data:
                path.write_bytes(data.replace(b"\r\n", b"\n"))
    report = dict(
        engine_commit=engine["commit"],
        patches=[],
        files=[],
        result="running",
        comparison="All pinned tracked files after patch replay; all tracked/nonignored new checkout files; ignored extra files under src. CRLF normalized.",
    )
    for name in engine["patches"]:
        patch = root / name
        apply_patch(tree, patch)
        report["patches"].append(dict(path=name, sha256=hashlib.sha256(patch.read_bytes()).hexdigest()))
    expected = {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}
    listed = (
        subprocess.check_output(
            ["git", "-C", str(checkout), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        )
        .decode()
        .split("\0")
    )
    actual = {name for name in listed if name and (checkout / name).is_file()}
    actual.update(
        path.relative_to(checkout).as_posix()
        for path in (checkout / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")
    )
    # Git submodule entries represent a dependency, not an engine source file.
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    changed = []
    for name in sorted(expected & actual):
        wanted = (tree / name).read_bytes().replace(b"\r\n", b"\n")
        built = (checkout / name).read_bytes().replace(b"\r\n", b"\n")
        if wanted != built:
            changed.append(name)
        report["files"].append(dict(path=name, normalized_sha256=hashlib.sha256(built).hexdigest()))
    report.update(
        missing=missing,
        extra=extra,
        changed=changed,
        result="failed" if missing or extra or changed else "passed",
        finished_utc=datetime.now(timezone.utc).isoformat(),
    )
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(output / "report.json")
    if report["result"] != "passed":
        raise RuntimeError(
            "Engine checkout differs from replay: " + json.dumps(dict(missing=missing, extra=extra, changed=changed))
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--engine-root", type=Path, help="Compare a separate committed fork without changing production"
    )
    args = parser.parse_args()
    run(
        args.source_root,
        args.output
        or args.source_root / "artifacts/patch-verification" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"),
        args.engine_root,
    )
