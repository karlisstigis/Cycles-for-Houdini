# SPDX-License-Identifier: Apache-2.0
"""One fresh-process producer queue shared by catalogue and domain recipe items."""

import json
import os
import shutil
import sys
import uuid

import pytest

from support.catalogue import read_queue_result, required_cases
from support.paths import ROOT, artifact_path
from support.workers import run_process


@pytest.fixture(scope="session")
def catalogue_queue(request):
    # Session items have already passed -k/-m and explicit node selection.
    selected = [
        item.callspec.params[parameter]
        for item in request.session.items
        if hasattr(item, "callspec")
        for parameter in ("catalogue_case", "recipe_case")
        if parameter in item.callspec.params
    ]
    expected = required_cases(selected)
    output = artifact_path("pytest-catalogue", uuid.uuid4().hex[:8], "selection.json").parent
    selection = output / "selection.json"
    selection.write_text(json.dumps(expected, indent=2))
    command = [
        shutil.which("pwsh") or "pwsh",
        "-NoProfile",
        "-File",
        str(ROOT / "scripts/run-tests.ps1"),
        "-Tier",
        "nightly",
        "-CatalogueSelectionFile",
        str(selection),
        "-Artifacts",
        str(output / "queue"),
        "-TimeoutSeconds",
        "900",
        "-FixtureArtifacts",
        str(output / "fixtures"),
    ]
    for option, parameter in (
        ("--build-directory", "-BuildDirectory"),
        ("--houdini-root", "-HoudiniRoot"),
        ("--test-dependencies", "-TestDependencies"),
    ):
        value = request.config.getoption(option)
        if value:
            command += [parameter, str(value)]
    # Inherited verified contexts continue to select the same immutable build.
    if not request.config.getoption("--build-directory") and os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY"):
        from native_test_runtime import runtime_context

        command += ["-BuildDirectory", runtime_context()["build_directory"]]
    runner_error = None
    try:
        run_process(
            command,
            timeout=120 + len(expected) * 920,
            destination=output,
            env=dict(os.environ, CYCLES_HOUDINI_WORKER_LOG_DIR=str(output / "fixtures/workers")),
            cwd=output,
        )
    except AssertionError as error:
        # Preserve per-item failures from the completed runner, rather than
        # turning a single failed fixture into a failed session setup for all.
        runner_error = error
    try:
        entries = read_queue_result(output / "queue", expected, runner_failed=runner_error is not None)
    except Exception as error:
        if runner_error is not None:
            raise error from runner_error
        raise
    queue = next((output / "queue").glob("*/summary.json")).parent
    run_process(
        [
            sys.executable,
            str(ROOT / "scripts/native_test_runner.py"),
            "--audit",
            "--summary",
            str(queue / "summary.json"),
        ],
        timeout=120,
        destination=output / "audit",
        env=dict(os.environ),
        cwd=output,
    )
    for row in entries.values():
        if row.get("evidence"):
            row["evidence_path"] = queue / row["evidence"]
    return entries
