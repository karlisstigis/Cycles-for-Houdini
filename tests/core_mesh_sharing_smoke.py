# SPDX-License-Identifier: Apache-2.0
"""Physical surface mesh sharing, independent face slots and live splits/repairs."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse, json, os, subprocess, traceback
from pathlib import Path

device = argparse.ArgumentParser()
device.add_argument("--device", default="CPU")
device = device.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
path = artifacts_root() / "stage3/mesh-sharing/core-report.json"
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
            "--mesh-sharing-contracts",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    report.update(returncode=run.returncode, stdout=run.stdout, stderr=run.stderr)
    assert run.returncode == 0, report
    values = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert values and values[-1]["result"] == "passed" and values[-1]["reference_images"] >= 20, report
    report.update(result="passed", contracts=values[-1])
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(report["contracts"]))
