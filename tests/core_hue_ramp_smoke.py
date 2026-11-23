# SPDX-License-Identifier: Apache-2.0
"""Bounded hue recipes reject malformed ownership and recover unsafe colors."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json, os, subprocess
from pathlib import Path
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/hue-ramp-contracts"
destination.mkdir(parents=True, exist_ok=True)
result = subprocess.run(
    [str(runtime_executable("ch_harness.exe")), "--hue-ramp-contracts"],
    capture_output=True,
    text=True,
    check=True,
    timeout=30,
)
report = json.loads(result.stdout.strip().splitlines()[-1])
assert report["result"] == "passed" and report["hue_ramp_checks"] >= 30, report
(destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
