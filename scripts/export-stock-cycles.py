# SPDX-License-Identifier: Apache-2.0
"""Extract the pinned upstream Cycles commit without touching the production fork."""

import argparse
import hashlib
import io
from pathlib import Path, PurePosixPath
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    marker = args.destination / ".stock-commit"
    digest_marker = args.destination / ".stock-archive-sha256"
    existing = marker.exists()
    if existing and marker.read_text(encoding="ascii").strip() != args.commit:
        raise RuntimeError("Existing stock source has a different commit")
    bundle_link = args.destination / "lib/windows_x64"
    expected_bundle = (args.repository / "lib/windows_x64").resolve()
    if existing and bundle_link.exists():
        if bundle_link.is_junction():
            if bundle_link.resolve() != expected_bundle:
                raise RuntimeError("Stock dependency junction targets a different library bundle")
        elif not bundle_link.is_dir() or any(bundle_link.iterdir()):
            raise RuntimeError("Stock dependency directory is neither empty nor the pinned bundle junction")
    if not existing and any(args.destination.iterdir()):
        raise RuntimeError("Stock source directory is not empty")
    resolved = subprocess.check_output(
        ["git", "-C", str(args.repository), "rev-parse", f"{args.commit}^{{commit}}"], text=True
    ).strip()
    if resolved != args.commit:
        raise RuntimeError("Requested commit did not resolve exactly")
    archive = subprocess.check_output(["git", "-C", str(args.repository), "archive", "--format=tar", args.commit])
    archive_digest = hashlib.sha256(archive).hexdigest()
    if existing and digest_marker.exists() and digest_marker.read_text(encoding="ascii").strip() != archive_digest:
        raise RuntimeError("Existing stock source archive digest changed")
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        members = source.getmembers()
        if any(
            PurePosixPath(item.name).is_absolute()
            or ".." in PurePosixPath(item.name).parts
            or (
                item.name.rstrip("/") != "lib/windows_x64"
                and not (args.destination / item.name).resolve().is_relative_to(args.destination.resolve())
            )
            for item in members
        ):
            raise RuntimeError("Upstream archive contains an unsafe path")
        if existing:
            expected = {item.name.rstrip("/") for item in members}
            allowed = expected | {".stock-commit", ".stock-archive-sha256", "lib/windows_x64"}
            pending = [args.destination]
            while pending:
                directory = pending.pop()
                for item in directory.iterdir():
                    relative = item.relative_to(args.destination).as_posix()
                    if relative not in allowed:
                        raise RuntimeError(f"Unexpected stock build input: {relative}")
                    if item.is_dir() and relative != "lib/windows_x64":
                        pending.append(item)
            for item in members:
                if item.isfile():
                    target = args.destination / item.name
                    recorded = hashlib.sha256(source.extractfile(item).read()).digest()
                    if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != recorded:
                        raise RuntimeError(f"Pristine stock source changed: {item.name}")
        else:
            source.extractall(args.destination, filter="data")
    marker.write_text(args.commit + "\n", encoding="ascii")
    digest_marker.write_text(archive_digest + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
