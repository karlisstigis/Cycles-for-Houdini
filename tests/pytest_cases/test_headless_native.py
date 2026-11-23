# SPDX-License-Identifier: Apache-2.0
"""Nightly native scripts run in fresh, verified host processes without a GUI."""

import json
import os
from pathlib import Path
import sys

import pytest

from support.paths import ROOT, artifact_path
from support.workers import run_process

pytestmark = [pytest.mark.hython, pytest.mark.slow]

CASES = [
    pytest.param("core_image_iris_smoke.py", "CPU", marks=[pytest.mark.cpu, pytest.mark.release_gate]),
    pytest.param("core_image_iris_smoke.py", "OPTIX", marks=[pytest.mark.optix, pytest.mark.release_gate]),
    pytest.param("hython_scene_contract.py", "CPU", marks=pytest.mark.cpu),
    pytest.param("hython_scene_contract.py", "OPTIX", marks=pytest.mark.optix),
]


@pytest.mark.parametrize("script,device", CASES)
def test_native_headless(script, device):
    assert "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ, "Native tests require scripts/run-tests.ps1 -Tier nightly"
    assert Path(sys.executable).name.lower() == "hython.exe", "Use Houdini hython for native discovery"
    destination = artifact_path("pytest-native", Path(script).stem + "-" + device, "report.json")
    env = dict(os.environ, CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(destination.with_suffix(".identity.json")))
    env["PYTHONPATH"] = os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES", "")
    # Do not inherit the source-injection done inside conftest. A native child
    # must load the package belonging to the verified build, as every tier does.
    run_process(
        [
            sys.executable,
            str(ROOT / "tests/native_test_entry.py"),
            str(ROOT / "tests" / script),
            "--device",
            device,
            "--report",
            str(destination),
        ],
        timeout=540,
        destination=destination.parent,
        env=env,
    )
    report = json.loads(destination.read_text())
    assert report["result"] == "passed", report
    if script == "core_image_iris_smoke.py":
        assert all(run["events"][-1]["feature_enabled"] for run in report["runs"]), report


@pytest.mark.cpu
@pytest.mark.release_gate
def test_native_snapshot_wire_codec_and_retained_ownership():
    assert "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ, "Snapshot wire contracts need a verified native build"
    destination = artifact_path("pytest-native", "snapshot-wire", "output").parent
    report = artifact_path("stage1", "snapshots", "core-snapshot-retention-smoke.json")
    env = dict(
        os.environ,
        PYTHONPATH=os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES", ""),
        CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(destination / "identity.json"),
    )
    run_process(
        [
            sys.executable,
            str(ROOT / "tests/native_test_entry.py"),
            str(ROOT / "tests/core_snapshot_retention_smoke.py"),
        ],
        timeout=120,
        destination=destination,
        env=env,
    )
    data = json.loads(report.read_text())
    assert data["result"] == "passed" and data["contracts"] >= 23
    assert data["objects"] == 100000 and data["retained_camera_states"] == 200
    assert data["wire_objects"] == 0


@pytest.mark.cpu
@pytest.mark.release_gate
def test_native_pybind_arguments_metadata_and_frame_ownership():
    assert "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ, "Binding contracts need a verified native build"
    assert Path(sys.executable).name.lower() == "hython.exe", "Use Houdini hython for native contracts"
    destination = artifact_path("pytest-native", "pybind-front", "output").parent
    env = dict(
        os.environ,
        PYTHONPATH=os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES", ""),
        CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(destination / "identity.json"),
    )
    run_process(
        [
            sys.executable,
            str(ROOT / "tests/native_test_entry.py"),
            str(ROOT / "tests/houdini_pybind_front_contract.py"),
        ],
        timeout=120,
        destination=destination,
        env=env,
    )


@pytest.mark.cpu
@pytest.mark.release_gate
def test_native_argument_buffer_ownership_and_rejection():
    assert "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ, "Buffer contracts need a verified native build"
    assert Path(sys.executable).name.lower() == "hython.exe", "Use Houdini hython for native contracts"
    destination = artifact_path("pytest-native", "argument-buffer", "output").parent
    report = destination / "report.json"
    env = dict(
        os.environ,
        PYTHONPATH=os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES", ""),
        CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(destination / "identity.json"),
    )
    run_process(
        [
            sys.executable,
            str(ROOT / "tests/native_test_entry.py"),
            str(ROOT / "tests/houdini_argument_buffer_contract.py"),
            "--report",
            str(report),
        ],
        timeout=120,
        destination=destination,
        env=env,
    )
    evidence = json.loads(report.read_text())
    assert evidence["result"] == "passed" and len(evidence["checks"]) >= 11, evidence
