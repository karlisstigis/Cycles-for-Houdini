# SPDX-License-Identifier: Apache-2.0
"""Exact bounded device transfers, per-owner readback, resize and rejection."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse, hashlib, json, os, subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path)
args = parser.parse_args()
selection = args.device
devices = json.loads(selection) if selection.startswith("[") else [selection]
destination = args.report or artifacts_root() / "stage3/performance/device-ranges/report.json"
destination.parent.mkdir(parents=True, exist_ok=True)
harness = runtime_executable("ch_harness.exe")
with harness.open("rb") as stream:
    harness_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
boards = []
if devices != ["CPU"] and os.environ.get("CYCLES_HOUDINI_VERIFY_RANGE_BOARDS") == "1":
    from session_soak_fixture import cuda_board

    boards = [cuda_board(device) for device in devices]
    assert len({board["uuid"] for board in boards}) == len(devices), boards
result = subprocess.run(
    [
        str(harness),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "|".join(devices),
        "--device-range-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=120,
)
events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
report = dict(
    result="passed" if result.returncode == 0 else "failed",
    devices=devices,
    boards=boards,
    harness=str(harness),
    harness_sha256=harness_sha256,
    events=events,
    stdout=result.stdout,
    stderr=result.stderr,
    exit_code=result.returncode,
)
destination.write_text(json.dumps(report, indent=2))
assert result.returncode == 0, (result.stdout, result.stderr)
assert events[-1]["result"] == "passed" and events[-1]["checks"] >= 70 and events[-1]["owner_readbacks"] >= 12, events
assert sorted(events[-1]["instantiated_devices"]) == sorted(devices), events
if devices != ["CPU"]:
    assert events[-1]["mapped_allocations"] == 4 and events[-1]["shared_host_aliases"] == 2, events
print(destination)
