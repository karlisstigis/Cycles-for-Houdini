# SPDX-License-Identifier: Apache-2.0
"""Owned camera optics contracts and independent static-camera image controls."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse, json, os, subprocess
from pathlib import Path

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path, default=artifacts_root() / "stage2/cameras/dof-motion/core-report.json")
args = p.parse_args()
devices = json.loads(args.device) if args.device.startswith("[") else [args.device]
command = [
    str(runtime_executable("ch_harness.exe")),
    "--resources",
    os.environ["CYCLES_HOUDINI_RESOURCES"],
    "--device",
    devices[0],
    "--camera-dof-motion-contracts",
    "--output",
    str(args.report.parent / "core-images"),
]
for device in devices[1:]:
    command += ["--camera-dof-device", device]
run = subprocess.run(command, capture_output=True, text=True, timeout=750)
events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
report = dict(
    result="passed" if run.returncode == 0 else "failed",
    devices=devices,
    events=events,
    stdout=run.stdout,
    stderr=run.stderr,
    exit_code=run.returncode,
    scope="Owned engine/wire contracts; actual temporal optics against independent static-camera exposure quadrature. Host/live capture is a separate gate.",
)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
assert run.returncode == 0, (run.stdout, run.stderr)
assert (
    events[-1]["result"] == "passed" and events[-1]["image_comparisons"] == 7 and events[-1]["rejected_inputs"] == 16
), events
assert events[-1]["closed_owned_bytes"] == events[-1]["closed_device_bytes"] == 0, events
