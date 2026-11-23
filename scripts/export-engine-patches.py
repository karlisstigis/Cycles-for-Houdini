# SPDX-License-Identifier: Apache-2.0
"""Export future engine commits without rewriting the accepted locked patch stack."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import runpy

ROOT = Path(__file__).resolve().parents[1]
METADATA = runpy.run_path(str(Path(__file__).with_name("engine_commit_metadata.py")))


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def next_patch_number(root, engine):
    names = list(engine["patches"])
    triage = root / "patches/triage.json"
    if triage.is_file():
        names.extend(row["path"] for row in json.loads(triage.read_text()).get("retired_patches", []))
    return max((int(Path(name).name.split("-", 1)[0]) for name in names), default=0) + 1


def record_authoring_tree(engine, tree):
    """Return engine lock fields with authoring_tree set to tree, placed right after authoring_tip."""
    result = {}
    for key, value in engine.items():
        if key != "authoring_tree":
            result[key] = value
        if key == "authoring_tip":
            result["authoring_tree"] = tree
    result.setdefault("authoring_tree", tree)
    return result


def export(root, repo, output, since=None):
    lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
    engine = lock["cycles"]
    since = since or engine.get("authoring_tip")
    if not since:
        raise RuntimeError("Set optional cycles.authoring_tip or pass --since for the accepted authoring tip")
    if engine.get("authoring_tip") and since != engine["authoring_tip"]:
        raise RuntimeError("--since must equal the locked authoring tip; historical patches are immutable")
    if output.exists():
        raise RuntimeError("Export target exists; refusing to overwrite accepted patches")
    if git(repo, "status", "--porcelain").strip():
        raise RuntimeError("Commit or preserve authoring work before export")
    git(repo, "merge-base", "--is-ancestor", engine["commit"], since)
    git(repo, "merge-base", "--is-ancestor", since, "HEAD")
    revision = since + "..HEAD"
    if git(repo, "rev-list", "--merges", revision).strip():
        raise RuntimeError("Export requires a linear feature history; review/rebase merges first")
    for commit, _, message in METADATA["commit_history"](repo, since, "HEAD"):
        METADATA["future_metadata"](root, repo, commit, message)
    tip = git(repo, "rev-parse", "HEAD").decode().strip()
    frozen = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in engine["patches"]}
    if "patch_sha256" in engine and frozen != engine["patch_sha256"]:
        raise RuntimeError("Accepted patch bytes differ from source.lock.json")
    inventory_path = root / "patches/inventory.json"
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
        by_name = {item["path"]: item["sha256"] for item in inventory["patches"]}
        if any(by_name.get(name) != digest for name, digest in frozen.items()):
            raise RuntimeError("Accepted patch bytes differ from inventory")
    patch_dir = output / "patches"
    patch_dir.mkdir(parents=True)
    git(
        repo,
        "format-patch",
        "--binary",
        "--full-index",
        "--no-signature",
        "--start-number=" + str(next_patch_number(root, engine)),
        "--output-directory=" + str(patch_dir.resolve()),
        revision,
    )
    patches = sorted(patch_dir.glob("*.patch"))
    expected_count = int(git(repo, "rev-list", "--count", revision))
    if len(patches) != expected_count:
        raise RuntimeError("Export count differs from authoring commit count")
    if not patches:
        raise RuntimeError("No future commits to export")
    # Verify actual exported bytes, including binary edits/deletions, against
    # HEAD without checking out over an artist session or touching its index.
    with tempfile.TemporaryDirectory() as temporary:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(temporary) / "replay.index")

        def index_git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], env=env)

        index_git("read-tree", since)
        for patch in patches:
            index_git("apply", "--cached", "--whitespace=nowarn", str(patch.resolve()))
        if index_git("write-tree").strip() != git(repo, "rev-parse", "HEAD^{tree}").strip():
            raise RuntimeError("Exported patches do not reproduce the authoring HEAD tree")
    proposal = copy.deepcopy(lock)
    exported = {"patches/" + path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in patches}
    proposal["cycles"]["patches"].extend(exported)
    proposal["cycles"]["patch_sha256"] = {**frozen, **exported}
    proposal["cycles"]["authoring_tip"] = tip
    # The public build route replays the patches onto cycles.commit and checks the
    # result against this tree, so it must move together with authoring_tip.
    tree = git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    proposal["cycles"] = record_authoring_tree(proposal["cycles"], tree)
    (output / "source.lock.proposed.json").write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8")
    report = dict(
        result="passed",
        since=since,
        tip=tip,
        authoring_tree=tree,
        exported=len(patches),
        exported_tree_matches_head=True,
        commit_metadata_validated=True,
        patches=[dict(path=name, sha256=digest) for name, digest in exported.items()],
        preserved_locked_patches=frozen,
        next_step="Review new patches and proposed lock, update inventory and ADR 0005 triage, then independently replay and qualify. Historical patch files are never regenerated.",
    )
    (output / "export.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--author-repo", type=Path, default=ROOT / "third_party/cycles")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--since", help="Accepted authoring tip if optional lock metadata is absent")
    args = parser.parse_args()
    result = export(args.source_root.resolve(), args.author_repo.resolve(), args.output.resolve(), args.since)
    print(json.dumps({key: value for key, value in result.items() if key != "preserved_locked_patches"}, indent=2))
