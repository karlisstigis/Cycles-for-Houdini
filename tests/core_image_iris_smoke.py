# SPDX-License-Identifier: Apache-2.0
"""Actual optional native aperture sampling, normalized energy and live file repairs."""

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
parser.add_argument("--report", type=Path, default=artifacts_root() / "image-iris/core-report.json")
args = parser.parse_args()
devices = json.loads(args.device) if args.device.startswith("[") else [args.device]
args.report.parent.mkdir(parents=True, exist_ok=True)
runs = []
for index, device in enumerate(devices):
    destination = args.report.parent / ("images-" + str(index))
    command = [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--image-iris-render",
        "--output",
        str(destination),
    ]
    run = subprocess.run(command, capture_output=True, text=True, timeout=420)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    comparison = destination / "comparison.json"
    runs.append(
        dict(
            device=device,
            exit_code=run.returncode,
            stdout=run.stdout,
            stderr=run.stderr,
            events=events,
            comparison=json.loads(comparison.read_text()) if comparison.exists() else None,
        )
    )
    args.report.write_text(json.dumps(dict(result="running", runs=runs), indent=2), encoding="utf-8")
    assert run.returncode == 0, (run.stdout, run.stderr)
    final = events[-1]
    assert final["result"] == "passed", events
    if final["feature_enabled"]:
        assert final["image_comparisons"] == 2 and final["rejected_inputs"] == 3, events
        assert final["camera_lookup_tables_reused"], events
        assert final["persistent_ipr"] and final["closed_owned_bytes"] == final["closed_device_bytes"] == 0, events
        assert (destination / "bokeh-comparison.png").is_file(), destination
        assert comparison.is_file(), destination
    else:
        assert final["analytic_render"] and final["unsupported_rejected"], events
args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            runs=runs,
            scope="Actual native persistent renders. Both methods sample the same normalized image. Uniform-field energy, emitted bokeh moments, lookup-table reuse for warm camera edits, shutter curve uploads and active image replacement and repair are checked. Render times are observations.",
        ),
        indent=2,
    ),
    encoding="utf-8",
)
print(args.report)
