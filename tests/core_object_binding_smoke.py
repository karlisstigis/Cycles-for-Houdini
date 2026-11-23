# SPDX-License-Identifier: Apache-2.0
"""Strict reference ownership across incremental updates and safe rejections."""

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
        "--object-binding-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=120,
)
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
assert report["result"] == "passed"
assert report["modes"] == 2 and report["safe_rejections"] == 38 and report["accepted_updates"] == 18
assert report["depth_reference_comparisons"] == 2
report.update(
    device=device,
    scope="Ordered and persistent engine updates; malformed/duplicate internal keys remain strict, all retained geometry references remain valid, and rebind/delete/recreate images match fresh depth references. Artist id/name attributes are independent.",
)
dest = artifacts_root() / "stage1/object-bindings"
dest.mkdir(parents=True, exist_ok=True)
(dest / "core-object-binding-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
