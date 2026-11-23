# SPDX-License-Identifier: Apache-2.0
"""Fresh verified children for native owner destruction and worker termination."""

import json
import os
from pathlib import Path
import sys

import pytest

from support.paths import ROOT, artifact_path
from support.workers import run_process

pytestmark = [pytest.mark.hython, pytest.mark.slow, pytest.mark.cpu]


@pytest.mark.parametrize(
    "case",
    (
        "frame-close",
        "concurrent-close",
        "worker-mapping",
        "display-close",
        "display-replace",
        "display-ordering",
        "transaction-close",
        "capture-retire-reader",
    ),
)
def test_native_lifetime(case):
    assert "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ, "Lifetime contracts need a verified native build"
    assert Path(sys.executable).name.lower() == "hython.exe", "Use run-hython.ps1 with pytest --native"
    destination = artifact_path("pytest-native", "lifetime", case, "report.json")
    env = dict(
        os.environ,
        PYTHONPATH=os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES", ""),
        HOUDINI_USER_PREF_DIR=str(destination.parent / "prefs/houdini__HVER__"),
        CYCLES_HOUDINI_TEST_ARTIFACTS=str(destination.parent),
        CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(destination.parent / "identity.json"),
    )
    run_process(
        [
            sys.executable,
            str(ROOT / "tests/native_test_entry.py"),
            str(ROOT / "tests/houdini_native_lifetime_contract.py"),
            "--case",
            case,
            "--device",
            "CPU",
            "--report",
            str(destination),
        ],
        timeout=120,
        destination=destination.parent,
        env=env,
    )
    report = json.loads(destination.read_text())
    assert report["result"] == "passed" and report["case"] == case, report
    assert report["evidence"]["recovery"] is True, report
