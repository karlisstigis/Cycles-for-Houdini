# SPDX-License-Identifier: Apache-2.0
"""Independent uniform-field ray-time, ownership and true-height references."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse
import json
import os
from pathlib import Path
import subprocess

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument(
    "--report", type=Path, default=artifacts_root() / "stage3/materials/instance-motion/core-report.json"
)
args = parser.parse_args()
devices = json.loads(args.device) if args.device.startswith("[") else [args.device]
command = [
    str(runtime_executable("ch_harness.exe")),
    "--resources",
    os.environ["CYCLES_HOUDINI_RESOURCES"],
    "--device",
    devices[0],
    "--instance-motion-contracts",
]
for device in devices[1:]:
    command += ["--instance-motion-device", device]
run = subprocess.run(command, capture_output=True, text=True, timeout=600)
events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
report = dict(
    result="passed" if run.returncode == 0 else "failed",
    devices=devices,
    events=events,
    stdout=run.stdout,
    stderr=run.stderr,
    exit_code=run.returncode,
    scope="Owned engine contract; independent corner/point/key field and already-displaced geometry controls. Host capture/live repair is a separate gate.",
)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
assert run.returncode == 0, (run.stdout, run.stderr)
assert events[-1]["result"] == "passed" and events[-1]["image_comparisons"] == 53, events
assert events[-1]["rejected_inputs"] == 16, events
assert events[-1]["closed_owned_bytes"] == events[-1]["closed_device_bytes"] == 0, events
assert any(item.get("check") == "129-sample descriptor image" for item in events), events
assert any(
    item.get("check") == "Camera uniform shutter table uses complete interval"
    and item["uniform_tables"] == item["nonuniform_parity_tables"] == 4
    for item in events
), events
print(args.report)
