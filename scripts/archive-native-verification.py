# SPDX-License-Identifier: Apache-2.0
"""Archive a completed native run and its verified sources before further edits."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import uuid


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Path must be inside {root}: {path}")
    return path


def source_path(root, name):
    if Path(name).is_absolute():
        raise ValueError("Manifest source paths must be relative")
    return inside(root, root / name)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def archive(source_root, run, destination, *, evidence_root=None):
    root = Path(source_root).resolve()
    verification = Path(evidence_root).resolve() if evidence_root is not None else root / "artifacts/verification"
    run, destination = inside(verification, run), inside(verification, destination)
    if run == destination or destination.is_relative_to(run):
        raise ValueError("Archive destination must be separate from the run")
    manifest_path = run / "build-manifest.json"
    manifest, summary, audit = (
        read(run / name) for name in ("build-manifest.json", "summary.json", "source-audit.json")
    )
    manifest_hash = digest(manifest_path)
    if audit["result"] != "passed" or audit.get("build_manifest_sha256", manifest_hash) != manifest_hash:
        raise ValueError("A completed source audit matching the run manifest is required")
    if summary["result"] not in ("passed", "failed") or any(
        t.get("exit_code") is None or t.get("result") not in ("passed", "failed") for t in summary["tests"]
    ):
        raise ValueError("Run must be terminal with explicit test exit codes")
    destination.mkdir(parents=True, exist_ok=True)
    sources = destination / ("sources-" + manifest_hash)
    if not sources.exists():
        # An interrupted copy remains identifiable evidence; never mutate an
        # already-published source archive or delete an uncertain directory.
        pending = destination / (".partial-sources-" + uuid.uuid4().hex)
        pending.mkdir()
        shutil.copy2(manifest_path, pending / "build-manifest.json")
        for entry in manifest["integration_source"]:
            source = source_path(root, entry["path"])
            if digest(source) != entry["sha256"]:
                raise ValueError(f"Source changed before archival: {entry['path']}")
            target = source_path(pending, entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if digest(target) != entry["sha256"]:
                raise ValueError(f"Source changed during archival: {entry['path']}")
        pending.rename(sources)
    if digest(sources / "build-manifest.json") != manifest_hash:
        raise ValueError("Archived manifest identity changed")
    for entry in manifest["integration_source"]:
        if digest(source_path(sources, entry["path"])) != entry["sha256"]:
            raise ValueError(f"Archived source identity changed: {entry['path']}")
    index_path = destination / "index.json"
    index = read(index_path) if index_path.exists() else dict(schema_version=1, runs=[])
    record = dict(
        run=(run.relative_to(root) if run.is_relative_to(root) else run).as_posix(),
        result=summary["result"],
        tests=summary["tests"],
        manifest_sha256=manifest_hash,
        source_archive=(sources.relative_to(root) if sources.is_relative_to(root) else sources).as_posix(),
    )
    index["runs"] = [r for r in index["runs"] if r["run"] != record["run"]] + [record]
    temporary = index_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    temporary.replace(index_path)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--evidence-root", type=Path, help="Explicit boundary for external run and archive evidence")
    args = parser.parse_args()
    print(json.dumps(archive(args.source_root, args.run, args.destination, evidence_root=args.evidence_root)))
