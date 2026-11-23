# SPDX-License-Identifier: Apache-2.0
"""Owned interpolation maps, geometric/motion preservation and bounded storage."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json, os, subprocess, traceback
from pathlib import Path

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
report = dict(result="running", events=[], children=[])
path = artifacts_root() / "stage3/performance/subdivision-shading/core-report.json"
path.parent.mkdir(parents=True, exist_ok=True)
try:
    for repeat in range(12):
        run = subprocess.run(
            [
                str(runtime_executable("ch_harness.exe")),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                "CPU",
                "--subdivision-shading-contracts",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        child = dict(repeat=repeat, returncode=run.returncode, stdout=run.stdout, stderr=run.stderr)
        report["children"].append(child)
        assert run.returncode == 0, child
        measured = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
        assert measured and measured[-1]["result"] == "passed" and measured[-1]["subdivision_shading_checks"] >= 80, (
            child
        )
        report["events"].extend(measured)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2))
print(path)
