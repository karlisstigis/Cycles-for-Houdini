# SPDX-License-Identifier: Apache-2.0
"""Sparse assembly, strict ownership, demand caching and protocol contracts."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse
import json
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--volume-vector-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=100,
)
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
from worker_protocol_fixture import expected_worker_protocol

assert (
    report["checks"] >= 55
    and report["strict_rejections"] == 5
    and report["worker_protocol"] == expected_worker_protocol()
), report
dest = artifacts_root() / "stage2/volumes/vector-components"
dest.mkdir(parents=True, exist_ok=True)
(dest / "contracts.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
