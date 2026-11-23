# SPDX-License-Identifier: Apache-2.0
"""Real SubPatch 64-bit arithmetic across 32-bit boundaries, with no large allocation.

This exercises patch 0075's triangle/vertex count math. The private DiagSplit
allocation-error paths and huge rendered subdivisions remain outside its scope.
Factors through 92682 cover the conservative UV-diagonal ceiling at the
integration's maximum supported subdivision level 16 (axis factor 65536).
"""

from support.paths import artifacts_root
import argparse
import json
from pathlib import Path
import subprocess

from native_test_runtime import runtime_executable, runtime_context

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--device", default="CPU", help="Launcher compatibility; this host arithmetic contract performs no device rendering"
)
parser.add_argument("--report", type=Path, default=artifacts_root() / "stage2/subdivision/count-overflow/report.json")
args = parser.parse_args()
destination = args.report.parent
destination.mkdir(parents=True, exist_ok=True)
run = subprocess.run(
    [
        str(runtime_executable()),
        "--resources",
        str(Path(runtime_context()["host"]) / "resources"),
        "--device",
        "CPU",
        "--subdivision-shading-contracts",
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
    assert measured[-1]["subdivision_count_overflow_checks"] >= 32, report
    report.update(result="passed", contracts=measured[-1])
finally:
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
