# SPDX-License-Identifier: Apache-2.0
"""Build and run native architecture contracts in the launcher's verified runtime.

Run through scripts/run-hython.ps1, native_test_entry.py or a native test tier.
No SDK version, build directory, production DLL or executable fallback is used.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from native_test_runtime import runtime_context


def cache_values(build):
    values = {}
    for line in (build / "CMakeCache.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"([^/#][^:]*):[^=]+=(.*)", line)
        if match:
            values[match[1]] = match[2]
    return values


def compiler_environment(cache, inherited):
    # Initialize the exact compiler toolset recorded by this configured build.
    compiler = Path(cache["CMAKE_CXX_COMPILER"])
    vc = next(parent for parent in compiler.parents if parent.name == "VC")
    devcmd = vc.parent / "Common7/Tools/VsDevCmd.bat"
    parts = compiler.parts
    version = parts[parts.index("MSVC") + 1]
    # Pass a batch file, preserving literal quotes through Windows argv parsing.
    with tempfile.TemporaryDirectory(prefix="ch-native-compiler-") as temporary:
        command = Path(temporary) / "environment.cmd"
        command.write_text(
            f'@echo off\ncall "{devcmd}" -no_logo -arch=x64 -host_arch=x64 -vcvars_ver={version} >nul\nif errorlevel 1 exit /b %errorlevel%\nset\n'
        )
        result = subprocess.run(["cmd", "/u", "/d", "/c", str(command)], env=inherited, check=True, capture_output=True)
    environment = dict(inherited)
    for line in result.stdout.decode("utf-16-le").splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            environment[name] = value
    # Ninja can be a system Python launcher. Keep the Houdini interpreter's
    # library paths only for the Houdini-side probes below.
    return {key: value for key, value in environment.items() if key.upper() not in {"PYTHONHOME", "PYTHONPATH"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true", help="Run targets already built by an earlier job")
    args = parser.parse_args()
    context = runtime_context()
    build = Path(context["build_directory"]).resolve()
    sdk = Path(context["houdini_root"]).resolve()
    host = Path(context["host"]).resolve()
    root = Path(__file__).resolve().parents[1]
    cache = cache_values(build)
    if Path(cache["HOUDINI_ROOT"]).resolve() != sdk:
        raise RuntimeError("Native contract SDK differs from the launcher's verified Houdini")
    if cache.get("CH_IMAGE_IRIS", "").upper() not in {"ON", "TRUE", "1"}:
        raise RuntimeError("Native contracts require the production Image Iris configuration")
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    logs = args.report.parent / (args.report.stem + "-logs")
    logs.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join(
        [str(sdk / "bin"), str(host / "bin"), str(build / "bin"), environment.get("PATH", "")]
    )
    environment["PYTHONHOME"] = str(sdk / "python313")
    report = {"result": "running", "runtime": context, "built": not args.skip_build, "cases": []}

    def run(name, command, *, env=environment, timeout=180):
        began = time.monotonic()
        result = subprocess.run(
            [str(item) for item in command],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        (logs / (name + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
        (logs / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
        report["cases"].append({"name": name, "returncode": result.returncode, "seconds": time.monotonic() - began})
        if result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); see {logs}")

    try:
        targets = [
            "ch_dicing_contracts",
            "ch_ipc_contracts",
            "ch_frame_publication_contracts",
            "ch_platform_contracts",
            "ch_python_bindings_contracts",
        ]
        if not args.skip_build:
            run(
                "build",
                [cache["CMAKE_COMMAND"], "--build", build, "--parallel", "4", "--target", *targets],
                env=compiler_environment(cache, environment),
                timeout=600,
            )
        binaries = []
        for target in targets[:-1]:
            executable = (build / "bin" / (target + ".exe")).resolve(strict=True)
            if not executable.is_relative_to(build):
                raise RuntimeError("Native contract executable escapes the selected build")
            run(target, [executable, logs] if target == "ch_platform_contracts" else [executable])
            binaries.append(executable)
        probe = (build / "binding-contracts/_cycles_houdini.pyd").resolve(strict=True)
        if not probe.is_relative_to(build):
            raise RuntimeError("Binding contract probe escapes the selected build")
        run(
            "python-bindings",
            [sdk / "python313/python.exe", root / "src/harness/python_bindings_contracts.py", probe.parent],
        )
        binaries.append(probe)
        # Keep the frozen legacy fixture independent of the production converter.
        run(
            "legacy-status-parity",
            [
                sdk / "python313/python.exe",
                root / "tests/python_status_conversion_contract.py",
                "--houdini-root",
                sdk,
                "--artifacts",
                logs / "legacy-status-parity",
            ],
        )
        report["binaries"] = [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in binaries
        ]
        report["result"] = "passed"
    except Exception as error:
        report["result"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": report["result"], "report": str(args.report), "contracts": len(report["cases"])}))


if __name__ == "__main__":
    main()
