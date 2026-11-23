# SPDX-License-Identifier: Apache-2.0
"""Engine displacement dependencies ignore surface-only changes and retain real invalidation."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
from pathlib import Path
import subprocess

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
result = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "CPU",
        "--displacement-hash-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=30,
)
assert result.returncode == 0, (result.stdout, result.stderr)
events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
assert events[-1]["result"] == "passed" and events[-1]["displacement_hash_checks"] == 8, events
destination = artifacts_root() / "stage3/displacement-hash/report.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(
    json.dumps(dict(result="passed", events=events, stderr=result.stderr), indent=2), encoding="utf-8"
)
print(destination)
