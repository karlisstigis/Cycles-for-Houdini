# SPDX-License-Identifier: Apache-2.0
"""Sparse pose transport, ownership rejection and independent fresh renders."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse
import json
import os
from pathlib import Path
import subprocess
from worker_protocol_fixture import expected_worker_protocol

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--object-pose-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=150,
)
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
assert (
    report["result"] == "passed"
    and report["objects"] == 100000
    and report["worker_protocol"] == expected_worker_protocol()
)
assert report["safe_rejections"] == 36 and report["fresh_comparisons"] == 12
assert report["pose_bytes"] * 2 < report["full_object_bytes"]
traces = [
    json.loads(line[len("CH_UPDATE_PROFILE ") :])
    for line in run.stderr.splitlines()
    if line.startswith("CH_UPDATE_PROFILE ")
]
if os.environ.get("CYCLES_HOUDINI_PROFILE_UPDATES") == "1":
    assert traces and sum(not row["success"] for row in traces) == 36
    for row in traces:
        assert row["total_seconds"] >= 0 and all(value >= 0 for value in row["stages"].values())
        assert abs(sum(row["stages"].values()) - row["total_seconds"]) <= max(1e-5, row["total_seconds"] * 1e-4)
else:
    assert not traces, "Normal rendering emitted opt-in timing records"
report["update_profile_records"] = len(traces)
report["device"] = device
destination = artifacts_root() / "stage1/object-poses"
destination.mkdir(parents=True, exist_ok=True)
(destination / "core-object-pose-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
