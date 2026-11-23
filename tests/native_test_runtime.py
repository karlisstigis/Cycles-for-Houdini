# SPDX-License-Identifier: Apache-2.0
"""Resolve auxiliary test executables from the launcher's verified build.

There is deliberately no SDK version or build-directory fallback. A test which
spawns a reference harness must inherit the same explicit context as its host.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def _verified_runtime():
    value = os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY")
    if not value:
        raise RuntimeError(
            "Missing verified test runtime; launch through scripts/run-hython.ps1, run-tests.ps1 or a verification runner"
        )
    context = json.loads(value)
    for name in ("build_directory", "manifest", "manifest_sha256", "houdini", "host"):
        if not context.get(name):
            raise RuntimeError("Verified test runtime is missing " + name)
    build = Path(context["build_directory"])
    manifest = Path(context["manifest"])
    host = Path(context["host"])
    if not build.is_absolute() or not manifest.is_absolute() or not host.is_absolute():
        raise RuntimeError("Verified test build, manifest and host must be absolute paths")
    if not build.is_dir() or not host.is_dir():
        raise RuntimeError("Verified test build and host must be existing directories")
    if manifest.resolve() != build.resolve() / "build-manifest.json":
        raise RuntimeError("Test manifest is outside the selected build")
    if not manifest.is_file():
        raise RuntimeError("Test manifest must be an existing file")
    # Hash and parse one immutable byte buffer. A path can be atomically
    # replaced between reads, so rereading it after verification could grant
    # authority to binary records which the launcher never selected.
    payload = manifest.read_bytes()
    expected_hash = context["manifest_sha256"]
    if hashlib.sha256(payload).hexdigest() != expected_hash.lower():
        raise RuntimeError("Test build manifest changed after runtime selection")
    data = json.loads(payload.decode("utf-8-sig"))
    if data["host_sdk"]["houdini"] != context["houdini"]:
        raise RuntimeError("Test build manifest and selected Houdini versions differ")
    return context, data


def runtime_context():
    return _verified_runtime()[0]


def build_directory():
    return Path(runtime_context()["build_directory"])


def build_manifest():
    return Path(runtime_context()["manifest"])


def runtime_executable(name="ch_harness.exe"):
    if name not in ("ch_harness.exe", "cycles.exe", "ch_gui_debugger.exe"):
        raise ValueError("Unsupported test executable: " + name)
    context, manifest = _verified_runtime()
    build = Path(context["build_directory"]).resolve()
    relative = "bin/" + name
    records = [
        item
        for item in manifest.get("binary_artifacts", [])
        if str(PurePosixPath(item.get("path", "").replace("\\", "/"))) == relative
    ]
    if len(records) != 1:
        raise RuntimeError("Selected build must manifest exactly one " + relative)
    executable = build / relative
    if not executable.resolve().is_relative_to(build):
        raise RuntimeError("Test executable escapes the selected build")
    if not executable.is_file():
        raise RuntimeError("Test executable must be an existing file: " + relative)
    if hashlib.sha256(executable.read_bytes()).hexdigest() != records[0]["sha256"].lower():
        raise RuntimeError("Test executable differs from selected build manifest: " + relative)
    return executable
