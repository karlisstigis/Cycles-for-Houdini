# SPDX-License-Identifier: Apache-2.0
"""Opt-in compiler regression for MultiDevice range-upload ownership failures.

The existing native opt-in marker keeps the mandatory pure gate independent of
installed C++ tools. This test builds only a standalone synthetic-child harness.
"""

import json

import pytest

from support.paths import ROOT, artifact_path
from support.workers import ordinary_python, pure_python_environment, run_process

pytestmark = [pytest.mark.hython, pytest.mark.cpu]


def test_multi_device_range_upload_rejects_missing_owner():
    output = artifact_path("multi-device-range-fault", "report.json").parent
    run_process(
        [
            str(ordinary_python()),
            str(ROOT / "tests" / "multi_device_range_fault_test.py"),
            "--output",
            str(output),
        ],
        timeout=300,
        destination=output / "execution",
        env=pure_python_environment(),
        cwd=ROOT,
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["result"] == "passed"
    runs = {run["name"]: run for run in report["runs"]}
    production = runs["production"]
    assert production["exit_code"] == 0
    assert production["checks"]["cases"] >= 27
    assert production["checks"]["checks"] >= 2956
    assert runs["missing-guard-control"]["exit_code"] != 0
    assert runs["parent-error-control"]["exit_code"] != 0
