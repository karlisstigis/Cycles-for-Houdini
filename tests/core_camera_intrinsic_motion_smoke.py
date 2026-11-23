# SPDX-License-Identifier: Apache-2.0
"""Owned full camera projection samples against independent static exposures."""

from support.paths import artifacts_root
from worker_protocol_fixture import expected_worker_protocol
from native_test_runtime import runtime_executable
import argparse, json, os, subprocess
from pathlib import Path

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path, default=artifacts_root() / "stage2/cameras/intrinsic-motion/core-report.json")
args = p.parse_args()
devices = json.loads(args.device) if args.device.startswith("[") else [args.device]
command = [
    str(runtime_executable("ch_harness.exe")),
    "--resources",
    os.environ["CYCLES_HOUDINI_RESOURCES"],
    "--device",
    devices[0],
    "--camera-intrinsic-motion-contracts",
    "--output",
    str(args.report.parent / "core-images"),
]
for device in devices[1:]:
    command += ["--camera-intrinsic-device", device]
run = subprocess.run(command, capture_output=True, text=True, timeout=900)
events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
report = dict(
    result="passed" if run.returncode == 0 else "failed",
    devices=devices,
    events=events,
    stdout=run.stdout,
    stderr=run.stderr,
    exit_code=run.returncode,
    scope="Actual selected-device rendering; full projection payload, source recovery, strict wire/schema and independent static-camera quadrature. Host/live source workflow is separate.",
)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
assert run.returncode == 0, (run.stdout, run.stderr)
assert (
    events[-1]["result"] == "passed" and events[-1]["image_comparisons"] == 13 and events[-1]["rejected_inputs"] == 16
), events
assert (
    events[-1]["closed_owned_bytes"] == events[-1]["closed_device_bytes"] == 0
    and events[-1]["worker_protocol"] == expected_worker_protocol()
), events
assert (
    events[-1]["camera_transform_rejections"] == 4
    and events[-1]["camera_displacement_comparisons"] == 2
    and events[-1]["camera_origin_bound_witnesses"] == 2
), events[-1]

assert sorted(events[-1]["render_device_ids"]) == sorted(devices), events[-1]
