# SPDX-License-Identifier: Apache-2.0
"""Immutable object-table ownership and exact worker deltas at 100k instances."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
from pathlib import Path
import subprocess

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "CPU",
        "--snapshot-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=60,
)
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
assert report["result"] == "passed" and report["contracts"] >= 23 and report["objects"] == 100000, report
assert report["retained_camera_states"] == 200 and report["wire_objects"] == 0
report["scope"] = (
    "Owned snapshot tables, exact sparse deltas and wire transport; rendering correctness and UI latency have separate native tests."
)
dest = artifacts_root() / "stage1/snapshots"
dest.mkdir(parents=True, exist_ok=True)
(dest / "core-snapshot-retention-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
