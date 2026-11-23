# SPDX-License-Identifier: Apache-2.0
"""CPU analytic texels and strict ownership/recovery for memory image loaders."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse, json, os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        args.device,
        "--owned-image-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=90,
)
out = artifacts_root() / "stage3/textures/owned-engine"
out.mkdir(parents=True, exist_ok=True)
(out / "stdout.log").write_text(run.stdout, encoding="utf-8")
(out / "stderr.log").write_text(run.stderr, encoding="utf-8")
if run.returncode:
    (out / "report.json").write_text(
        json.dumps(dict(result="failed", exit_code=run.returncode, error=run.stderr), indent=2), encoding="utf-8"
    )
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout.splitlines()[-1])
assert report["result"] == "passed"
assert report["analytic_texels"] == 7 and report["safe_rejections"] == 7
assert report["image_delta_checks"] >= 17
assert report["portable_conversion_checks"] == 81970
report["device"] = args.device
(out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
