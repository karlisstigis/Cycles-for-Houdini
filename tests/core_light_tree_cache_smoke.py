# SPDX-License-Identifier: Apache-2.0
"""Bit-exact light-tree cache records and strict malformed-block rejection."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json, os, subprocess
from pathlib import Path

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
result = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "CPU",
        "--light-tree-cache-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=60,
)
assert result.returncode == 0, (result.stdout, result.stderr)
events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
assert events[-1]["result"] == "passed" and events[-1]["checks"] >= 500 and events[-1]["blocks"] >= 100, events
destination = artifacts_root() / "stage3/performance/light-tree-compaction/core-report.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(dict(result="passed", events=events, stderr=result.stderr), indent=2))
print(destination)
