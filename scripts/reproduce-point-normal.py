# SPDX-License-Identifier: Apache-2.0
"""Run the engine-owned point-normal regression before and after isolated 0003.

Requires Python, Git, C++20 and TBB headers. No Houdini SDK or installed renderer.
The archived input trees, compiler logs, executables and raw JSON stay in output.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
BASE = "74e9d9e48f86330987d7392166a51b3652d7c557"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    if args.toolchain_cmd:
        initialized = subprocess.check_output(
            [environment.get("ComSpec", "cmd.exe"), "/d", "/c", str(args.toolchain_cmd.resolve())],
            text=True,
        )
        for line in initialized.splitlines():
            key, separator, value = line.partition("=")
            if separator and key:
                environment[key] = value
    archive_bytes = subprocess.check_output(
        ["git", "-C", str(args.cycles_root.resolve()), "archive", "--format=tar", BASE]
    )
    patch = args.patch.resolve()
    source = ROOT / "tests/upstream/point_nonuniform_normal.cpp"
    msvc = Path(args.compiler).stem.lower() in {"cl", "clang-cl"}
    report = dict(
        result="running",
        upstream_commit=BASE,
        patch=str(patch),
        patch_sha256=digest(patch),
        fixture_sha256=digest(source),
        compiler=args.compiler,
        configurations=[],
        scope="Actual CPU point intersection/shader/object/motion kernel headers; analytic ellipsoid normal and directional diffuse-response oracle. No full integrator, Embree or GPU rendering asserted.",
    )
    for label in ("before", "after"):
        tree = output / label
        tree.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            for member in archive.getmembers():
                (tree / member.name).resolve().relative_to(tree)
                if member.issym() or member.islnk():
                    raise RuntimeError("Unsupported archive link: " + member.name)
            archive.extractall(tree, filter="data")
        for path in tree.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                if b"\0" not in content and b"\r\n" in content:
                    path.write_bytes(content.replace(b"\r\n", b"\n"))
        if label == "after":
            isolated = environment.copy()
            for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
                isolated.pop(name, None)
            isolated["GIT_CEILING_DIRECTORIES"] = str(tree.parent)
            subprocess.run(
                ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
                cwd=tree,
                input=patch.read_bytes().replace(b"\r\n", b"\n"),
                env=isolated,
                check=True,
            )
        executable = output / (label + (".exe" if msvc else ""))
        includes = [tree / "src", tree / "third_party/atomic", args.tbb_include.resolve()]
        if msvc:
            command = [
                args.compiler,
                "/nologo",
                "/std:c++20",
                "/EHsc",
                "/O2",
                "/DNOMINMAX",
                *["/I" + str(path) for path in includes],
                str(source),
                "/Fo" + str(output / (label + ".obj")),
                "/Fe" + str(executable),
                "/link",
                "/NODEFAULTLIB:tbb12.lib",
            ]
        else:
            command = [
                args.compiler,
                "-std=c++20",
                "-O2",
                *["-I" + str(path) for path in includes],
                str(source),
                "-o",
                str(executable),
            ]
        compiled = subprocess.run(command, env=environment, capture_output=True, text=True)
        (output / (label + "-compile.log")).write_text(compiled.stdout + compiled.stderr, encoding="utf-8")
        if compiled.returncode:
            raise RuntimeError("Compilation failed; see " + str(output / (label + "-compile.log")))
        executed = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
        (output / (label + ".json")).write_text(executed.stdout, encoding="utf-8")
        measured = json.loads(executed.stdout)
        report["configurations"].append(
            dict(
                label=label,
                command=command,
                returncode=executed.returncode,
                measured=measured,
                point_header_sha256=digest(tree / "src/kernel/geom/point_intersect.h"),
            )
        )
    before, after = report["configurations"]
    passed = (
        before["returncode"] == 1
        and before["measured"]["failures"] == 144
        and before["measured"]["control_failures"] == 0
        and after["returncode"] == 0
        and after["measured"]["failures"] == 0
        and before["measured"]["checks"] == after["measured"]["checks"] == 504
    )
    report["result"] = "passed" if passed else "failed"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(result=report["result"], report=str(output / "report.json"))))
    if not passed:
        raise RuntimeError("Expected stock failure, fixed success and unaffected controls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles-root", type=Path, required=True)
    parser.add_argument("--tbb-include", type=Path, required=True)
    parser.add_argument("--compiler", default="cl.exe" if os.name == "nt" else "c++")
    parser.add_argument(
        "--toolchain-cmd", type=Path, help="Optional Windows developer-environment initializer ending with set"
    )
    parser.add_argument("--patch", type=Path, default=ROOT / "patches/0003-point-nonuniform-normals.patch")
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
