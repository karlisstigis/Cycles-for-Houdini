# SPDX-License-Identifier: Apache-2.0
"""Two native owners preserve frames on rejection, detach and persistent updates."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
from pathlib import Path
import subprocess

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/session-lifecycle"
destination.mkdir(parents=True, exist_ok=True)
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "CPU",
        "--session-lifecycle-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=240,
)
report = dict(
    result="failed", scope=__doc__, device="CPU", returncode=run.returncode, stdout=run.stdout, stderr=run.stderr
)
if run.returncode == 0:
    values = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert values and values[-1]["session_lifecycle_contracts"] == "passed", report
    assert values[-1]["checks"] >= 30, report
    report.update(result="passed", contracts=values[-1], update_modes=["synchronous", "persistent"])
(destination / "core-session-lifecycle-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
assert report["result"] == "passed", report
print(json.dumps(report))
