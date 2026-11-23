# SPDX-License-Identifier: Apache-2.0
"""Independent particle/GSplat lifetime integrals, instances and attribute removal.

Uses the existing real engine fixture for patch 0005; run CPU and each intended
GPU backend explicitly. This report only qualifies its named execution devices.
"""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import subprocess

from native_test_runtime import runtime_executable

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path, default=artifacts_root() / "stage2/particles/lifetime/report.json")
args = parser.parse_args()
devices = json.loads(args.device) if args.device.startswith("[") else [args.device]
args.report.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", scope=__doc__, runs=[])
try:
    for index, device in enumerate(devices):
        output = args.report.parent / ("images-" + str(index))
        run = subprocess.run(
            [
                str(runtime_executable()),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                device,
                "--point-lifetime-test",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
        row = dict(device=device, exit_code=run.returncode, events=events, stdout=run.stdout, stderr=run.stderr)
        report["runs"].append(row)
        assert run.returncode == 0, row
        assert events and events[-1]["result"] == "passed", row
        assert len([event for event in events if event.get("check") == "Particle shadow lifetime"]) == 3, row
        assert any(event.get("check") == "Remove lifetime attribute" for event in events), row
        assert len([event for event in events if event.get("check") == "GSplat lifetime"]) == 6, row
    report["result"] = "passed"
except BaseException:
    report["result"] = "failed"
    raise
finally:
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(args.report)
