# SPDX-License-Identifier: Apache-2.0
"""Many logical bindings, shared metadata, leader retirement and adaptive fan-out."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse, json, os, subprocess, traceback
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
path = artifacts_root() / "stage3/mesh-family-scaling/core-report.json"
path.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, scope=__doc__)
try:
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--mesh-family-scale",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    report.update(returncode=run.returncode, stdout=run.stdout, stderr=run.stderr)
    assert run.returncode == 0, report
    rows = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert rows and rows[-1]["result"] == "passed" and rows[-1]["logical_counts"] == [1000, 10000, 100000], report
    phases = [row for row in rows if "phase" in row]
    assert len(phases) == 32 and len([row for row in phases if row["adaptive"]]) == 8, phases
    report.update(result="passed", contracts=rows[-1], phases=phases)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(report["contracts"]))
