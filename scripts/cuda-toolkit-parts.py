# SPDX-License-Identifier: Apache-2.0
"""Gets only the parts of the NVIDIA CUDA Toolkit that the build needs, without NVIDIA's installer.

The build compiles the GPU kernels with nvcc (CUDA compiler), the CUDA headers and cudart.lib
(scripts/build.ps1 -CudaRoot, CMake's FindCUDA). NVIDIA publishes exactly these parts as separate
"redistributable" ZIP archives next to the full toolkit:
  https://developer.download.nvidia.com/compute/cuda/redist/redistrib_<version>.json
This tool downloads three of them (cuda_nvcc, cuda_cudart, cuda_cccl), checks each against the
size and SHA-256 pinned below (copied from NVIDIA's manifest), and unpacks them into ONE folder
of your own (default: <project>\\_work\\cuda-<version>, inside the project folder; until 22 November 2026
%LOCALAPPDATA%\\CyclesForHoudiniTools\\cuda-<version>). It runs nothing from
the archives, needs no administrator permission, changes no system setting (no CUDA_PATH, no
PATH) and cannot install, update or replace a display driver: the archives are plain ZIP files of
toolkit files, and NVIDIA's manifest has no Windows driver component at all.

By downloading you accept NVIDIA's CUDA Toolkit licence: https://docs.nvidia.com/cuda/eula/index.html
The files are used on your computer only; nothing of them goes into an installer.

  python scripts/cuda-toolkit-parts.py status [--root R]   # JSON: is a complete, checked copy there?
  python scripts/cuda-toolkit-parts.py get [--root R]      # download, check and unpack (safe to repeat)
  python scripts/cuda-toolkit-parts.py plan                # JSON: what "get" would download
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "12.8.0"  # CUDA 12.8 GA, the compiler the maintainer's builds use; the lock's cuda_toolkit is "12.8"
LOCK_TOOLKIT = "12.8"
BASE_URL = "https://developer.download.nvidia.com/compute/cuda/redist/"
MANIFEST = f"redistrib_{VERSION}.json"
LICENCE_URL = "https://docs.nvidia.com/cuda/eula/index.html"
# From NVIDIA's redistrib_12.8.0.json (release_date 2025-01-23).
PARTS = (
    {
        "name": "cuda_nvcc",
        "what": "CUDA compiler (nvcc) and its compiler headers",
        "version": "12.8.61",
        "path": "cuda_nvcc/windows-x86_64/cuda_nvcc-windows-x86_64-12.8.61-archive.zip",
        "sha256": "d0158c4dd6565c0c9bca50d2e0bd5fd944d40333725a1c6875ef1840b719abfc",
        "size": 120682751,
    },
    {
        "name": "cuda_cudart",
        "what": "CUDA runtime headers and cudart.lib",
        "version": "12.8.57",
        "path": "cuda_cudart/windows-x86_64/cuda_cudart-windows-x86_64-12.8.57-archive.zip",
        "sha256": "2c7aa62a195d79229d4381c8bd0174a30502cf3d8124c6e94ee50a7fc8a1e9f4",
        "size": 3034859,
    },
    {
        "name": "cuda_cccl",
        "what": "CUDA C++ core library headers (needed by the CUDA headers)",
        "version": "12.8.55",
        "path": "cuda_cccl/windows-x86_64/cuda_cccl-windows-x86_64-12.8.55-archive.zip",
        "sha256": "e218372c742d1ff2df9fbef82803e36c4fb05cffb51e9f123b380ad9c51e6965",
        "size": 2911311,
    },
)
MARKER = "cycles-cuda-parts.json"
# Files that must exist for scripts/build.ps1 (nvcc, FindCUDA's cudart.lib, the CUDA headers).
REQUIRED_FILES = ("bin/nvcc.exe", "bin/nvcc.profile", "include/cuda.h", "include/cuda_runtime.h", "lib/x64/cudart.lib")
# Driver files: a ZIP of toolkit parts never contains these; if one ever did, nothing is unpacked.
DRIVER_SUFFIXES = (".sys", ".inf", ".cat")


class PartsError(RuntimeError):
    pass


def default_root() -> Path:
    # The same folder the one-file builder uses (scripts/easy-common.ps1, Get-EasyCudaPartsFolder).
    return Path(__file__).resolve().parents[1] / "_work" / f"cuda-{VERSION}"


def total_bytes() -> int:
    return sum(part["size"] for part in PARTS)


def plan() -> dict:
    return {
        "version": VERSION,
        "lock_toolkit": LOCK_TOOLKIT,
        "manifest": BASE_URL + MANIFEST,
        "licence": LICENCE_URL,
        "download_bytes": total_bytes(),
        "parts": [dict(part, url=BASE_URL + part["path"]) for part in PARTS],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def status(root: Path) -> dict:
    """Complete = marker written by this tool for this version and parts, and the required files exist."""
    result = {"root": str(root), "version": VERSION, "complete": False, "missing": []}
    marker = root / MARKER
    if not marker.is_file():
        result["missing"] = [MARKER]
        return result
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["missing"] = [MARKER + " (unreadable)"]
        return result
    expected = {part["name"]: part["sha256"] for part in PARTS}
    if recorded.get("version") != VERSION or recorded.get("parts") != expected:
        result["missing"] = [MARKER + " (other version or parts)"]
        return result
    result["missing"] = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    result["complete"] = not result["missing"]
    return result


def _member_target(name: str) -> PurePosixPath | None:
    """Path inside the toolkit folder for one ZIP member, without the archive's top folder.

    Refuses absolute paths, drive letters and "..", so nothing lands outside the folder."""
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
        raise PartsError(f"Archive member with an absolute path: {name}")
    parts = PurePosixPath(clean).parts
    if any(part == ".." for part in parts):
        raise PartsError(f"Archive member outside its folder: {name}")
    if len(parts) < 2:
        return None  # the top folder itself
    return PurePosixPath(*parts[1:])


def check_archive(archive: Path) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    with zipfile.ZipFile(archive) as bundle:
        members = []
        tops = set()
        for info in bundle.infolist():
            clean = info.filename.replace("\\", "/")
            tops.add(clean.split("/", 1)[0])
            target = _member_target(info.filename)
            if target is None:
                continue
            if target.suffix.lower() in DRIVER_SUFFIXES:
                raise PartsError(f"{archive.name} contains a driver file ({info.filename}); nothing was unpacked")
            members.append((info, target))
        if len(tops) != 1:
            raise PartsError(f"{archive.name} does not have exactly one top folder")
        return members


# A busy connection or server: every download is tried this many times, with a short growing pause in between
# (2, 4, 8, 16 s). A partial file is continued where it stopped when the server allows it (HTTP Range), else
# started again; the size and the SHA-256 from NVIDIA's manifest stay the judge, and a file that fails them is
# deleted and downloaded again.
ATTEMPTS = 5
FIRST_PAUSE = 2.0
NOT_ANSWERED = (
    "The download from NVIDIA did not answer, your internet connection or NVIDIA's server may be busy: "
    "double-click the file again (nothing is left half-installed)."
)


class DownloadError(PartsError):
    pass


def _fetch_once(url: str, partial: Path, size: int, log, opener) -> None:
    have = partial.stat().st_size if partial.is_file() else 0
    if have > size:
        partial.unlink()
        have = 0
    headers = {"User-Agent": "cycles-for-houdini-build"}
    if 0 < have < size:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    with opener(request, timeout=120) as response:
        status = getattr(response, "status", None) or response.getcode()
        resumed = have > 0 and status == 206
        if have and not resumed:
            log("      starting this file again")
        done = have if resumed else 0
        with partial.open("ab" if resumed else "wb") as handle:
            step = max(size // 10, 1)
            mark = (done // step + 1) * step
            for block in iter(lambda: response.read(1 << 20), b""):
                handle.write(block)
                done += len(block)
                if done >= mark:
                    log(f"      {min(done * 100 // size, 100)} %")
                    mark += step


def _download(url: str, destination: Path, size: int, sha256: str, log, opener=None, sleep=None) -> None:
    import time

    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    if destination.is_file() and destination.stat().st_size == size and sha256_file(destination) == sha256:
        log(f"   already downloaded: {destination.name}")
        return
    partial = destination.with_name(destination.name + ".partial")
    log(f"   downloading {url} ({size / 1e6:.0f} MB)")
    pause = FIRST_PAUSE
    for attempt in range(1, ATTEMPTS + 1):
        try:
            _fetch_once(url, partial, size, log, opener)
            if partial.stat().st_size == size and sha256_file(partial) == sha256:
                os.replace(partial, destination)
                return
            if partial.stat().st_size >= size:
                partial.unlink()  # complete but not what NVIDIA's manifest says: download it again
                problem = "size or SHA-256 differs from NVIDIA's manifest"
            else:
                problem = f"stopped after {partial.stat().st_size} of {size} bytes"
        except (OSError, EOFError, ValueError) as error:  # URLError, timeouts, reset connections, short reads
            problem = str(getattr(error, "reason", None) or error) or type(error).__name__
        if attempt < ATTEMPTS:
            log(f"      try {attempt} of {ATTEMPTS} did not finish ({problem}); trying again in {pause:.0f} s")
            sleep(pause)
            pause *= 2
    log(f"      the last try did not finish either ({problem})")
    raise DownloadError(NOT_ANSWERED)


def get(root: Path, downloads: Path | None = None, source: str | None = None, log=print) -> dict:
    """Download (or take from `source`, a folder or URL with the same layout), check and unpack."""
    current = status(root)
    if current["complete"]:
        log(f"   already complete: {root}")
        return current
    if root.exists() and any(root.iterdir()):
        raise PartsError(
            f"{root} exists but is not a complete copy made by this tool; move it aside or choose another --root"
        )
    downloads = downloads or root.parent / (root.name + ".downloads")
    downloads.mkdir(parents=True, exist_ok=True)
    archives = []
    for part in PARTS:
        target = downloads / PurePosixPath(part["path"]).name
        if source and not source.startswith(("http://", "https://")):
            local = Path(source) / part["path"]
            shutil.copyfile(local, target)
            if target.stat().st_size != part["size"] or sha256_file(target) != part["sha256"]:
                raise PartsError(f"{local}: size or SHA-256 differs from NVIDIA's manifest")
        else:
            _download((source or BASE_URL).rstrip("/") + "/" + part["path"], target, part["size"], part["sha256"], log)
        archives.append((part, target))
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=root.name + ".incomplete-", dir=root.parent))
    try:
        for part, archive in archives:
            members = check_archive(archive)
            log(f"   unpacking {part['name']} {part['version']} ({len(members)} entries)")
            with zipfile.ZipFile(archive) as bundle:
                for info, target in members:
                    path = staging.joinpath(*target.parts)
                    if info.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists() and target.name.upper() != "LICENSE":
                        raise PartsError(f"Two archives contain {target}")
                    with bundle.open(info) as source_handle, path.open("wb") as handle:
                        shutil.copyfileobj(source_handle, handle)
        missing = [name for name in REQUIRED_FILES if not staging.joinpath(*name.split("/")).is_file()]
        if missing:
            raise PartsError("The unpacked parts lack " + ", ".join(missing))
        record = {
            "version": VERSION,
            "parts": {part["name"]: part["sha256"] for part in PARTS},
            "source": source or BASE_URL,
            "licence": LICENCE_URL,
            "about": "Made by scripts/cuda-toolkit-parts.py from NVIDIA's redistributable archives. Delete the folder to remove it.",
        }
        (staging / MARKER).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if root.exists():
            root.rmdir()  # empty (checked above)
        os.replace(staging, root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    for _, archive in archives:
        archive.unlink()
    try:
        downloads.rmdir()
    except OSError:
        pass
    return status(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "get"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=None, help="toolkit folder (default: %(default)s)")
        if name == "get":
            command.add_argument("--source", default=None, help=argparse.SUPPRESS)  # tests: a local mirror
    sub.add_parser("plan")
    sub.add_parser("default-root")
    args = parser.parse_args(argv)
    if args.command == "plan":
        print(json.dumps(plan(), indent=2))
        return 0
    if args.command == "default-root":
        print(default_root())
        return 0
    root = (args.root or default_root()).absolute()
    if args.command == "status":
        print(json.dumps(status(root), indent=2))
        return 0
    try:
        result = get(root, source=args.source, log=lambda text: print(text, file=sys.stderr, flush=True))
    except (PartsError, OSError, zipfile.BadZipFile) as error:
        print(f"NOT DONE: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
