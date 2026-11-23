# SPDX-License-Identifier: Apache-2.0
"""Analytic curvature, duplicate welding and finite/degenerate owned mesh inputs."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json, os, subprocess
from pathlib import Path

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
r = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "CPU",
        "--mesh-pointiness-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=30,
)
assert r.returncode == 0, (r.stdout, r.stderr)
events = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
assert events[-1]["result"] == "passed" and events[-1]["pointiness_checks"] == 12, events
destination = artifacts_root() / "stage3/materials/pointiness/core-report.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(dict(result="passed", events=events, stderr=r.stderr), indent=2))
print(destination)
