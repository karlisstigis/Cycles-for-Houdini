# SPDX-License-Identifier: Apache-2.0
"""IES text releases after its last graph owner without growing the intern table."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse, json, os, subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--ies-ownership-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=60,
)
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
assert report["result"] == "passed" and report["profiles"] == 64 and report["released"] == 66
assert report["ustring_after"] <= report["ustring_before"] + 128 * 1024
dest = artifacts_root() / "stage3/light-shaders"
dest.mkdir(parents=True, exist_ok=True)
(dest / "ownership-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
