# SPDX-License-Identifier: Apache-2.0
"""Independent shutter tests before connecting Houdini geometry time sampling."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
from pathlib import Path
import subprocess

import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/motion"
destination.mkdir(parents=True, exist_ok=True)
reports = []
for device in ("CPU", ch.default_device()):
    output = destination / ("core-cpu" if device == "CPU" else "core-optix")
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--motion-test",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if run.returncode:
        raise RuntimeError(f"Motion harness failed for {device} (exit {run.returncode})\n{run.stdout}\n{run.stderr}")
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert events and events[-1]["result"] == "passed", run.stdout
    checks = [event for event in events if "check" in event]
    assert len(checks) == 18, checks
    layout = next(event for event in checks if event.get("renders") == 25)
    assert layout["result"] == "passed", layout
    reports.append(
        dict(
            device=device,
            checks=checks,
            core_result=events[-1],
            stderr=run.stderr,
            images=[str(path) for path in sorted(output.glob("*.exr"))],
        )
    )
report = dict(
    result="passed",
    fixtures=reports,
    scope="Shared engine only; native shutter/FPS, correspondence and volume velocity remain separate gates",
)
(destination / "core-motion-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "core-motion-smoke.json")
