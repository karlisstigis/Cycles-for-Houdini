# SPDX-License-Identifier: Apache-2.0
"""Real renderer eligibility reference/candidate, CPU pixels and deterministic rejection."""

import json
import os
from pathlib import Path
import re
import subprocess

from native_test_runtime import runtime_executable
from support.paths import artifacts_root

destination = artifacts_root() / "stage20/attribute-eligibility"
destination.mkdir(parents=True, exist_ok=True)
environment = dict(os.environ, CYCLES_HOUDINI_PROFILE_UPDATES="1")
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        "CPU",
        "--attribute-eligibility-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=180,
    env=environment,
)
(destination / "stdout.log").write_text(run.stdout)
(destination / "stderr.log").write_text(run.stderr)
print(run.stdout, end="")
print(run.stderr, end="")
assert run.returncode == 0, (run.returncode, str(destination))
assert "CH_ATTRIBUTE_ELIGIBILITY mode=parallel records=8256 chunks=64" in run.stderr, "parallel branch unexercised"
assert "CH_ATTRIBUTE_ELIGIBILITY mode=serial records=8256 chunks=1" in run.stderr, "reference branch unexercised"
assert "ELIGIBILITY_TBB controlled=1" in run.stderr
match = re.search(r"ELIGIBILITY_TBB default=(\d+)", run.stderr)
assert match and int(match[1]) > 1, "default-concurrency branch did not allow multiple TBB participants"
fallback_case = run.stderr.split("ELIGIBILITY_CASE fallback-before-later-error\n", 1)[1].split("ELIGIBILITY_CASE ", 1)[
    0
]
assert fallback_case.count("CH_ATTRIBUTE_FALLBACK reason=attribute_count") == 2, fallback_case
error_case = run.stderr.split("ELIGIBILITY_CASE error-before-later-fallback\n", 1)[1].split("ELIGIBILITY_CASE ", 1)[0]
assert "CH_ATTRIBUTE_FALLBACK" not in error_case, error_case
steady_blur_case = run.stderr.split("ELIGIBILITY_CASE steady-blur-eligible\n", 1)[1].split(
    "SAMPLED_ATTRIBUTE_CASE ", 1
)[0]
assert "CH_ATTRIBUTE_ELIGIBILITY mode=serial records=8256 chunks=1" in steady_blur_case, steady_blur_case
assert "CH_ATTRIBUTE_ELIGIBILITY mode=parallel records=8256 chunks=64" in steady_blur_case, steady_blur_case
report = next(
    json.loads(line) for line in run.stdout.splitlines() if line.startswith('{"attribute_eligibility_contracts":true')
)
assert report["records"] == 8256 and report["rejected"] >= 10 and report["rendered_pairs"] >= 10, report
assert report["sampled_pairs"] >= 11, report
(destination / "report.json").write_text(json.dumps(dict(result="passed", **report), indent=2))
print(json.dumps(dict(result="passed", report=str(destination / "report.json"))))
