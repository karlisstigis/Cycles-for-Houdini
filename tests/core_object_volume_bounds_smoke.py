# SPDX-License-Identifier: Apache-2.0
"""Exact volume overlap broad phase against the old linear predicate and real object flag passes."""

from support.paths import artifacts_root
import argparse
import json
from pathlib import Path
import subprocess

from native_test_runtime import runtime_executable, runtime_context

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path, default=artifacts_root() / "stage2/objects/volume-bounds/report.json")
args = parser.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
run = subprocess.run(
    [
        str(runtime_executable()),
        "--resources",
        str(Path(runtime_context()["host"]) / "resources"),
        "--device",
        args.device,
        "--object-volume-bounds-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=90,
)
report = dict(result="failed", returncode=run.returncode, stdout=run.stdout, stderr=run.stderr, scope=__doc__)
try:
    assert run.returncode == 0, report
    measured = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert measured and measured[-1]["result"] == "passed", report
    assert measured[-1]["queries"] >= 18000 and measured[-1]["flag_passes"] == 6, report
    report.update(result="passed", contracts=measured[-1])
finally:
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
