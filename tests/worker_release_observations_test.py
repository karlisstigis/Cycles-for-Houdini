# SPDX-License-Identifier: Apache-2.0
"""Execute the complete worker command loop with a deterministic renderer double.

No renderer, Houdini, GPU or OS shared-memory implementation is loaded. The real
worker owns command dispatch, observation cursors, replies and acknowledgements.
The double makes a final frame ready exactly while command 14 is dispatched.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from multi_device_range_fault_test import compiler_environment

ROOT = Path(__file__).resolve().parents[1]
GUARD = "if (!closing && input.command != ch::ipc::release_input_command)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker-source", type=Path, default=ROOT / "src/worker/main.cpp")
    parser.add_argument("--vs-dev-cmd", type=Path)
    parser.add_argument(
        "--mutation-control", action="store_true", help="Also prove the pre-fix guard loses the final frame"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    worker = args.worker_source.read_text(encoding="utf-8")
    # Replace dependencies, never the command loop. Public declarations remain
    # the real ones; fake definitions are linked below the unmodified main body.
    body = re.sub(r"^#include[^\n]*\n", "", worker, flags=re.MULTILINE)
    harness_path = ROOT / "tests/support/worker_release_observations_harness.cpp"
    harness = harness_path.read_text(encoding="utf-8")
    prefix = "#define main worker_main\n"
    seam = "// @WORKER_MAIN@"
    assert harness.count(seam) == 1
    variants = {"production": body}
    if args.mutation_control and worker.count(GUARD) == 1:
        variants["pre-fix-control"] = body.replace(GUARD, "if (!closing)")
    report = {
        "result": "running",
        "scope": "complete production worker main; fake renderer, codecs, control pipe and mappings",
        "worker_source": str(args.worker_source.resolve()),
        "worker_source_sha256": hashlib.sha256(worker.encode()).hexdigest(),
        "test_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "harness_source_sha256": hashlib.sha256(harness_path.read_bytes()).hexdigest(),
        "mutation_control_requested": args.mutation_control,
        "runs": [],
    }
    environment = compiler_environment(args.vs_dev_cmd)
    try:
        for name, code in variants.items():
            cpp = output / (name + ".cpp")
            cpp.write_text(harness.replace(seam, prefix + code + "\n#undef main\n"), encoding="utf-8")
            exe = output / (name + (".exe" if os.name == "nt" else ""))
            if os.name == "nt":
                command = [
                    "cl",
                    "/nologo",
                    "/std:c++20",
                    "/EHsc",
                    "/W4",
                    "/WX",
                    "/D_CRT_SECURE_NO_WARNINGS",  # Match the production worker's MSVC CRT policy.
                    "/I" + str(ROOT / "include"),
                    "/I" + str(ROOT / "src/platform"),
                    str(cpp),
                    "/Fe:" + str(exe),
                    "/Fo:" + str(output / (name + ".obj")),
                ]
            else:
                command = [
                    shutil.which("c++") or "c++",
                    "-std=c++20",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I" + str(ROOT / "include"),
                    "-I" + str(ROOT / "src/platform"),
                    str(cpp),
                    "-o",
                    str(exe),
                ]
            hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            build = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120, **hidden)
            (output / (name + "-build.log")).write_text(build.stdout + build.stderr, encoding="utf-8")
            if build.returncode:
                raise RuntimeError(name + " compilation failed; see build log")
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20, **hidden)
            row = dict(name=name, exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr)
            report["runs"].append(row)
            if name == "production" and run.returncode:
                raise RuntimeError("Production worker failed behavioral assertions: " + run.stderr)
            if name != "production" and run.returncode == 0:
                raise RuntimeError("Pre-fix negative control unexpectedly passed")
        if args.mutation_control and len(variants) != 2:
            raise RuntimeError("Requested mutation control guard changed; review its seam")
        report["result"] = "passed"
    except Exception as error:
        report.update(result="failed", error=str(error))
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
