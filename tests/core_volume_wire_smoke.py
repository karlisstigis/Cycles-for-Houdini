# SPDX-License-Identifier: Apache-2.0
"""Protocol 88 volume transport: exact raw sparse grids, serial/parallel identity, chunking, fallbacks."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse
import json
import os
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device

run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--volume-wire-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=300,
)
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
from worker_protocol_fixture import expected_worker_protocol

assert (
    report["result"] == "passed"
    and report["checks"] >= 60
    and report["rejected"] == 3
    and report["opaque_rejected"] == 6
    and report["opaque_bytes"] > 1024
    and report["worker_protocol"] == expected_worker_protocol()
), report
dest = artifacts_root() / "stage2/volumes/wire"
dest.mkdir(parents=True, exist_ok=True)
(dest / "contracts.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
