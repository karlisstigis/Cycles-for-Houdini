# SPDX-License-Identifier: Apache-2.0
"""Review dispositions and generated evidence stay separate from execution claims."""

import ast
import json
from pathlib import Path

import pytest

from support.paths import ROOT, configure_test_environment

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_every_original_unmapped_case_has_reviewed_execution_requirements():
    review = json.loads((ROOT / "tests/test-dispositions.json").read_text())
    # The original 172 dispositions plus the K33 Blender/shutter gates and
    # their shared reference helper and the temporal Sample Count regression
    # and five Blender reference workflow contracts and their fixture, plus the timeline GUI controls
    # and live IPR benchmark, plus the direct-member preparation and packed-population
    # reuse contracts and the public-path renderer reuse diagnostic remain explicitly reviewed.
    # Independent Shape Frame/detail isolation, its timeline GUI gate and fixture.
    # Retain the DLSS sample-count disposition and add five Threads runtime/support/build entries.
    # The artist-scene Blender hull review and viewport frame benchmark each require a human.
    assert len(review["entries"]) == 204
    for name, record in review["entries"].items():
        assert (ROOT / "tests" / name).is_file()
        assert record["purpose"] and record["reason"]
        assert "result" not in record, "A disposition is not a passing execution"
        if record["category"] in ("gui", "benchmark", "manual"):
            assert record["requires_human"] and not record.get("automation")
        if record["category"] == "automated":
            assert record["automation"]["executor"] in (
                "pure",
                "powershell",
                "native",
                "native-review",
                "scripts/run-k33-gates.ps1",
            )


def test_official_headless_scripts_have_no_checkout_artifact_expressions():
    catalogue = json.loads((ROOT / "tests/test-catalogue.json").read_text())
    for case in catalogue["headless"]:
        name = case["test"]
        tree = ast.parse((ROOT / "tests" / name).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, str)
            ):
                continue
            if node.right.value == "artifacts" or node.right.value.startswith("artifacts/"):
                # These paths describe the intentionally disposable source tree
                # tested by archive retention, never the implementation checkout.
                assert name == "native_verification_archive_test.py" and ast.unparse(node.left) == "self.root", (
                    name,
                    node.lineno,
                )


def test_temporary_files_follow_selected_artifacts(monkeypatch, tmp_path):
    import os
    import tempfile
    import sys

    monkeypatch.setenv("CYCLES_HOUDINI_TEST_ARTIFACTS", str(tmp_path / "selected"))
    for key in ("TMP", "TEMP", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "CYCLES_HOUDINI_WORKER_LOG_DIR"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.delenv("CYCLES_HOUDINI_WORKER_LOG_DIR", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    directory = configure_test_environment()
    with tempfile.TemporaryDirectory() as created:
        assert Path(created).parent == directory == tmp_path / "selected/temp"
    assert os.environ["TMP"] == os.environ["TEMP"] == os.environ["TMPDIR"] == str(directory)
    assert sys.dont_write_bytecode


def test_worker_logs_use_selected_directory_and_keep_installed_default(monkeypatch, tmp_path):
    from cycles_houdini.ipr.queued import _worker_log_directory

    monkeypatch.setenv("CYCLES_HOUDINI_SOURCE_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("CYCLES_HOUDINI_WORKER_LOG_DIR", raising=False)
    assert _worker_log_directory() == tmp_path / "data/artifacts/workers"
    monkeypatch.setenv("CYCLES_HOUDINI_WORKER_LOG_DIR", str(tmp_path / "evidence/workers"))
    assert _worker_log_directory() == tmp_path / "evidence/workers"


def test_reviewed_automation_has_real_adapter_and_preserved_nonautomated_categories():
    review = json.loads((ROOT / "tests/test-dispositions.json").read_text())["entries"]
    catalogue = json.loads((ROOT / "tests/test-catalogue.json").read_text())
    adapters = {
        "pure": (ROOT / "tests/pytest_cases/test_legacy_contracts.py").read_text(),
        "powershell": (ROOT / "tests/pytest_cases/test_powershell_contracts.py").read_text(),
    }
    for name, record in review.items():
        category = "scheduled" if record["category"] == "automated" else record["category"]
        assert name in catalogue[category], (name, category)
        recipe = record.get("automation", {})
        executor = recipe.get("executor")
        if executor in adapters:
            assert name in adapters[executor], "Review marked an unregistered contract automated: " + name
        elif executor == "native-review":
            assert name in (ROOT / "tests" / recipe["owner"]).read_text()
        elif executor == "native":
            assert recipe["backends"] and len(recipe["backends"]) == len(set(recipe["backends"]))
            assert all(backend in ("CPU", "OPTIX", "script-declared") for backend in recipe["backends"])
        elif executor == "scripts/run-k33-gates.ps1":
            assert name in (ROOT / executor).read_text(), "Focused gate does not dispatch its reviewed test: " + name


def test_configure_preserves_explicit_worker_log_destination(monkeypatch, tmp_path):
    import os
    import tempfile
    import sys

    monkeypatch.setenv("CYCLES_HOUDINI_TEST_ARTIFACTS", str(tmp_path / "selected"))
    monkeypatch.setenv("CYCLES_HOUDINI_WORKER_LOG_DIR", str(tmp_path / "explicit-workers"))
    for key in ("TMP", "TEMP", "TMPDIR", "PYTHONDONTWRITEBYTECODE"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    configure_test_environment()
    assert os.environ["CYCLES_HOUDINI_WORKER_LOG_DIR"] == str(tmp_path / "explicit-workers")
