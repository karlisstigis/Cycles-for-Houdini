# SPDX-License-Identifier: Apache-2.0
"""Check or format tracked project sources using the pinned development formatters."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def tool(name, fallback, version):
    executable = shutil.which(name) or (str(fallback) if fallback.is_file() else None)
    if not executable:
        raise SystemExit(f"Install {name} {version} or pass its executable path")
    result = subprocess.check_output([executable, "--version"], text=True)
    if version != result.strip().split()[-1]:
        raise SystemExit(f"Expected {name} {version}; found {result.strip()}")
    return executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--staged", action="store_true", help="Check the exact blobs in the Git index")
    parser.add_argument("--ruff", default="ruff")
    parser.add_argument("--clang-format", default="clang-format")
    args = parser.parse_args()
    if args.staged and args.write:
        parser.error("--staged is a read-only check")
    if args.staged:
        common = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT, text=True
        ).strip()
        settings = Path(common) / "houdini-ci.json"
        if settings.is_file():
            config = json.loads(settings.read_text(encoding="utf-8-sig"))
            args.ruff = config.get("ruff", args.ruff)
            args.clang_format = config.get("clang", args.clang_format)
    vs = Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio/2022/BuildTools/VC/Tools/Llvm/x64/bin"
    clang = tool(args.clang_format, vs / "clang-format.exe", "18.1.8")
    ruff = tool(args.ruff, Path("/nonexistent"), "0.14.3")
    selection = ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if args.staged else ["ls-files", "-z"]
    files = subprocess.check_output(
        ["git", *selection, "--", "include", "src", "houdini", "blender", "scripts", "tests", "examples"], cwd=ROOT
    )
    owned = [
        name
        for name in files.decode().split("\0")
        if name and not name.startswith(("include/third_party/", "tests/fixtures/"))
    ]
    cpp = [name for name in owned if Path(name).suffix in (".cpp", ".c", ".cc", ".h", ".hpp", ".hh", ".inc")]
    python = [name for name in owned if name.endswith(".py")]
    if args.staged:
        for name in cpp + python:
            blob = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
            command = (
                [clang, "--dry-run", "--Werror", "--assume-filename=" + name]
                if name in cpp
                else [ruff, "format", "--check", "--no-cache", "--stdin-filename", name, "-"]
            )
            subprocess.run(command, input=blob, cwd=ROOT, check=True)
        return
    for command, paths in (
        ([clang, "-i"] if args.write else [clang, "--dry-run", "--Werror"], cpp),
        ([ruff, "format", "--no-cache"] + ([] if args.write else ["--check"]), python),
    ):
        for index in range(0, len(paths), 80):
            subprocess.run([*command, *paths[index : index + 80]], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
