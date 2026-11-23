# SPDX-License-Identifier: Apache-2.0
"""Opt-in standalone compiler contract; no renderer, Houdini or GPU is loaded."""

import json

import pytest

from support.paths import ROOT, artifact_path
from support.workers import ordinary_python, pure_python_environment, run_process

# Keep installed compiler tools optional, matching the existing standalone
# MultiDevice contract. The native marker does not imply GPU execution.
pytestmark = [pytest.mark.hython, pytest.mark.cpu]


def test_input_release_preserves_completed_worker_observations():
    output = artifact_path("worker-release-observations", "report.json").parent
    run_process(
        [str(ordinary_python()), str(ROOT / "tests/worker_release_observations_test.py"), "--output", str(output)],
        timeout=300,
        destination=output / "execution",
        env=pure_python_environment(),
        cwd=ROOT,
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["result"] == "passed"
    runs = {run["name"]: run for run in report["runs"]}
    assert runs["production"]["exit_code"] == 0
    checks = json.loads(runs["production"]["stdout"])
    assert checks == dict(scenarios=4, release_replies=8, polls=8, result="passed")
