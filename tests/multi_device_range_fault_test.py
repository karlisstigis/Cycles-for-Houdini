# SPDX-License-Identifier: Apache-2.0
"""Compile exact range-upload methods with synthetic owners and deterministic map loss.

No renderer, SDK, production DLL, installed worker, or GPU is built or loaded.
The executable checks production MultiDevice translation, aggregation and range
dispatch; backend behavior is instrumented and does not qualify physical GPUs.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from native_compiler import find_vs_dev_cmd

ROOT = Path(__file__).resolve().parents[1]


def method(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def prepare_source(engine):
    paths = {
        "multi": engine / "src/device/multi/device.cpp",
        "memory": engine / "src/device/memory.cpp",
        "range": engine / "src/device/memory_range.h",
        "device": engine / "src/device/device.cpp",
        "device_header": engine / "src/device/device.h",
        "light": engine / "src/scene/light.cpp",
    }
    sources = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    definitions = {
        "RANGE_VALIDATION": ("range", "inline size_t device_memory_validate_ranges("),
        "RANGE_POLICY": ("range", "enum class DeviceMemoryRangePolicy"),
        "HAVE_ERROR": ("device_header", "bool have_error()"),
        "SET_ERROR": ("device", "void Device::set_error("),
        "COPY_TO": ("memory", "void device_memory::device_copy_to()"),
        "COPY_TO_RANGES": ("memory", "bool device_memory::device_copy_to_ranges("),
        "LIGHT_TREE_UPLOAD_RESULT": ("light", "struct LightTreeUpload"),
        "LIGHT_TREE_UPLOAD": ("light", "static LightTreeUpload light_tree_upload("),
        "PTR_MAP_FIND": ("multi", "device_ptr ptr_map_find("),
        "PTR_MAP_CONTAINS": ("multi", "bool ptr_map_contains("),
        "FIND_OWNER": ("multi", "SubDevice *find_suitable_mem_device("),
        "ERROR_MESSAGE": ("multi", "const string &error_message() override"),
        "SUPPORTED": ("multi", "bool mem_copy_to_range_supported("),
        "TRANSFER": ("multi", "void mem_copy_to_range("),
    }
    source = (ROOT / "tests/support/multi_device_range_fault_harness.cpp").read_text(encoding="utf-8")
    for name, (file, signature) in definitions.items():
        declaration = method(sources[file], signature)
        if name in ("RANGE_POLICY", "LIGHT_TREE_UPLOAD_RESULT"):
            declaration += ";"
        source = source.replace("// @" + name + "@", declaration)
    return source, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths.values()}


def compiler_environment(explicit):
    if os.name != "nt":
        return dict(os.environ)
    devcmd = find_vs_dev_cmd(explicit)
    with tempfile.TemporaryDirectory(prefix="ch-range-fault-env-") as temporary:
        batch = Path(temporary) / "environment.cmd"
        batch.write_text(
            f'@echo off\ncall "{devcmd}" -no_logo -arch=x64 -host_arch=x64 >nul\nif errorlevel 1 exit /b %errorlevel%\nset\n'
        )
        result = subprocess.run(["cmd", "/u", "/d", "/c", str(batch)], check=True, capture_output=True)
    environment = dict(os.environ)
    for line in result.stdout.decode("utf-16-le").splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            environment[name] = value
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=ROOT / "third_party/cycles")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vs-dev-cmd", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    source, hashes = prepare_source(args.engine.resolve())
    environment = compiler_environment(args.vs_dev_cmd)
    run_environment = dict(os.environ)
    run_environment.pop("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_RANGE_UPLOAD", None)
    report = {
        "result": "running",
        "scope": "compiled production methods with synthetic child backends",
        "source_sha256": hashes,
        "runs": [],
    }
    # Mutation controls prove that successful compilation is insufficient and
    # that reverting to a parent-only diagnostic loses the error after queries.
    guard = """      const device_ptr pointer = ptr_map_find(*owner, key);
      if (!pointer) {
        /* Report on the child so MultiDevice::error_message retains the error,
         * including when an earlier island already received this range. */
        owner->device->set_error("Missing owned device allocation for range copy");
        return;
      }"""
    # Compile/run the supplied production source even when the guard is absent:
    # an old engine must fail executable assertions rather than a text check.
    mutation_seam = source.count(guard) == 1
    variants = {"production": source}
    if mutation_seam:
        variants.update(
            {
                "missing-guard-control": source.replace(
                    guard, "      const device_ptr pointer = ptr_map_find(*owner, key);"
                ),
                "parent-error-control": source.replace(
                    'owner->device->set_error("Missing owned device allocation for range copy")',
                    'this->set_error("Missing owned device allocation for range copy")',
                ),
            }
        )
    try:
        for name, contents in variants.items():
            cpp = output / (name + ".cpp")
            cpp.write_text(contents, encoding="utf-8")
            exe = output / (name + (".exe" if os.name == "nt" else ""))
            if os.name == "nt":
                command = [
                    "cl",
                    "/nologo",
                    "/std:c++20",
                    "/EHsc",
                    "/W4",
                    "/WX",
                    "/D_CRT_SECURE_NO_WARNINGS",
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
                    str(cpp),
                    "-o",
                    str(exe),
                ]
            build = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
            (output / (name + "-build.log")).write_text(build.stdout + build.stderr, encoding="utf-8")
            if build.returncode:
                raise RuntimeError(f"{name} failed compilation; see build log")
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=run_environment)
            row = {
                "name": name,
                "exit_code": run.returncode,
                "stdout": run.stdout,
                "stderr": run.stderr,
                "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            }
            report["runs"].append(row)
            if name == "production":
                if run.returncode:
                    raise RuntimeError(f"Production executable failed: {run.stderr}")
                row["checks"] = json.loads(run.stdout)
                assert (
                    row["checks"]["result"] == "passed" and row["checks"]["checks"] > 0 and row["checks"]["cases"] > 0
                ), row["checks"]
            elif run.returncode == 0:
                raise RuntimeError(f"Mutation control unexpectedly passed: {name}")
        if not mutation_seam:
            raise RuntimeError("Update the explicit mutation seam after reviewing the production guard")
        report["result"] = "passed"
    except Exception as error:
        report["result"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"result": report["result"], "report": str(output / "report.json"), "checks": report["runs"][0]["checks"]}
        )
    )


if __name__ == "__main__":
    main()
