# SPDX-License-Identifier: Apache-2.0
"""Bounded mesh-payload references, exact arrays/bindings and strict ownership rejection."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
import subprocess
import traceback
from pathlib import Path

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
path = artifacts_root() / "stage3/stylesheet-storage/core-report.json"
path.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", events=[], children=[])
try:
    for repeat in range(6):
        run = subprocess.run(
            [
                str(runtime_executable("ch_harness.exe")),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                "CPU",
                "--mesh-payload-contracts",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        child = dict(repeat=repeat, returncode=run.returncode, stdout=run.stdout, stderr=run.stderr)
        report["children"].append(child)
        assert run.returncode == 0, child
        values = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
        assert values and values[-1]["result"] == "passed" and values[-1]["checks"] > 150, child
        report["events"].append(values[-1])
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], processes=len(report["events"]))))
