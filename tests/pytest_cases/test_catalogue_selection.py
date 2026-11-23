# SPDX-License-Identifier: Apache-2.0
"""Catalogue discovery and producer closure without Houdini or render jobs."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from support.catalogue import catalogue_cases, headless_recipes, read_queue_result, required_cases
from support.paths import ROOT
from support.workers import ordinary_python, pure_python_environment

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


# Backend identities added after the original 518 (each a legitimately new headless test):
# the fatal GPU reply check (installer candidate 7429a7a0) and the curve-mapping table check
# (independent curve-mapping rewrite, docs/dev/provenance/curve-mapping.md).
ADDED_IDENTITIES = [
    dict(test="houdini_fatal_gpu_render_smoke.py", device="script-declared"),
    dict(test="houdini_curve_mapping_table_smoke.py", device="script-declared"),
]


def test_catalogue_retains_all_original_headless_backend_identities():
    cases = catalogue_cases()
    assert len(cases) == 518 + len(ADDED_IDENTITIES) == 520
    assert len({(row["test"], row["device"]) for row in cases}) == 520
    assert all(row in cases for row in ADDED_IDENTITIES)
    assert len(headless_recipes()) == 319 + len(ADDED_IDENTITIES) == 321
    for case in cases:
        assert case in required_cases([case])


def test_producers_are_backend_matched_deduplicated_and_ordered():
    selected = [dict(test="houdini_texture_auto_batch_smoke.py", device="OPTIX")]
    assert required_cases(selected) == [dict(test="houdini_texture_smoke.py", device="OPTIX"), *selected]
    selected = [dict(test="houdini_texture_cache_smoke.py", device="script-declared")]
    assert required_cases(selected) == [
        dict(test="houdini_texture_smoke.py", device="CPU"),
        dict(test="houdini_texture_smoke.py", device="OPTIX"),
        *selected,
    ]
    assert required_cases(selected * 2) == required_cases(selected)


def test_invalid_or_cyclic_producer_contracts_fail():
    recipes = [
        dict(test="one.py", backends=["CPU"], depends_on=["two.py"]),
        dict(test="two.py", backends=["CPU"], depends_on=["one.py"]),
    ]
    with pytest.raises(ValueError, match="Cyclic"):
        required_cases([dict(test="one.py", device="CPU")], recipes)
    with pytest.raises(ValueError, match="compatible"):
        required_cases([dict(test="one.py", device="OPTIX")], recipes)


def queue_fixture(tmp_path):
    selected = [dict(test="one.py", device="CPU")]
    output = tmp_path / "run"
    output.mkdir()
    manifest = b'{"test":true}'
    (output / "build-manifest.json").write_bytes(manifest)
    digest = hashlib.sha256(manifest).hexdigest()
    runtime = dict(host=str(tmp_path / "host"), houdini="22.0.466", manifest_sha256=digest)
    identity = dict(
        result="passed",
        test="one.py",
        houdini=runtime["houdini"],
        manifest_sha256=digest,
        selected_device=dict(backend="CPU", id="CPU"),
        **{field: str(tmp_path / "host" / field) for field in ("python", "native", "hda", "resources")},
    )
    (output / "one.identity.json").write_text(json.dumps(identity))
    row = dict(test="one.py", device="CPU", result="passed", exit_code=0, runtime_identity="one.identity.json")
    summary = dict(
        scope="native-catalogue-selection",
        tests=[row],
        plan=selected,
        result="passed",
        finished_utc="2026-11-09T00:00:00Z",
        runtime_identity=runtime,
        build_manifest_sha256=digest,
    )
    return output, selected, summary


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("scope", "scope"),
        ("missing", "exactly"),
        ("plan", "plan"),
        ("runner", "consistently"),
        ("digest", "manifest"),
        ("host", "outside"),
    ],
)
def test_queue_rejects_missing_cases_and_invalid_provenance(tmp_path, mutation, expected):
    output, selected, summary = queue_fixture(tmp_path)
    if mutation == "scope":
        summary["scope"] = "native-nightly"
    elif mutation == "missing":
        summary["tests"] = []
    elif mutation == "plan":
        summary["plan"] = []
    elif mutation == "runner":
        summary["runner_error"] = "audit/launcher failed"
    elif mutation == "digest":
        summary["runtime_identity"]["manifest_sha256"] = "0" * 64
    elif mutation == "host":
        identity = output / "one.identity.json"
        value = json.loads(identity.read_text())
        value["native"] = str(tmp_path / "other-host/native.pyd")
        identity.write_text(json.dumps(value))
    (output / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(AssertionError, match=expected):
        read_queue_result(tmp_path, selected)


def test_valid_queue_preserves_individual_failures_and_rejects_process_failure_with_all_passes(tmp_path):
    output, selected, summary = queue_fixture(tmp_path)
    path = output / "summary.json"
    path.write_text(json.dumps(summary))
    assert read_queue_result(tmp_path, selected)[("one.py", "CPU")]["result"] == "passed"
    with pytest.raises(AssertionError, match="consistently"):
        read_queue_result(tmp_path, selected, runner_failed=True)
    summary["result"] = "failed"
    summary["tests"][0].update(result="failed", exit_code=9)
    path.write_text(json.dumps(summary))
    assert read_queue_result(tmp_path, selected, runner_failed=True)[("one.py", "CPU")]["exit_code"] == 9


@pytest.mark.parametrize("reversed_part", ["plan", "tests"])
def test_queue_rejects_reordered_producer_and_consumer_evidence(tmp_path, reversed_part):
    output, selected, summary = queue_fixture(tmp_path)
    selected = [*selected, dict(test="two.py", device="CPU")]
    summary["plan"] = list(selected)
    summary["tests"] = [*summary["tests"], dict(summary["tests"][0], test="two.py")]
    summary[reversed_part].reverse()
    (output / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(AssertionError, match="plan|exactly"):
        read_queue_result(tmp_path, selected)


@pytest.mark.parametrize("options,enabled", [([], False), (["--native"], False), (["--catalogue"], True)])
def test_readonly_collection_is_opt_in_and_honors_deselection(tmp_path, options, enabled):
    command = [
        str(ordinary_python()),
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "tests/pytest_cases",
        "-k",
        "test_houdini_panel_motion_timing_smoke and houdini_panel_motion_timing_smoke-CPU",
        "--artifacts",
        str(tmp_path / "evidence"),
    ]
    command += options
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=dict(pure_python_environment([Path(pytest.__file__).resolve().parent.parent]), PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if enabled:
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("::test_houdini_panel_motion_timing_smoke[") == 1
        assert "houdini_panel_motion_timing_smoke-CPU" in result.stdout
    else:
        assert result.returncode == 5, result.stdout + result.stderr
        assert "::test_houdini_panel_motion_timing_smoke[" not in result.stdout
    assert not list((tmp_path / "evidence").rglob("selection.json")), "Collection dispatched the native queue"
