# SPDX-License-Identifier: Apache-2.0
"""Reviewed unmapped headless contracts; independent of official tier identities."""

import json
import os
from pathlib import Path
import sys

import pytest

from support.paths import ROOT, artifact_path, artifact_label
from support.workers import run_process

REVIEW = json.loads((ROOT / "tests/test-dispositions.json").read_text())
CASES = []
for name, record in REVIEW["entries"].items():
    recipe = record.get("automation", {})
    if recipe.get("executor") != "native":
        continue
    for backend in recipe["backends"]:
        marks = [pytest.mark.hython, pytest.mark.slow]
        marks.append(pytest.mark.optix if backend == "OPTIX" else pytest.mark.cpu)
        CASES.append(pytest.param(name, backend, recipe, id=name.removesuffix(".py") + "-" + backend, marks=marks))


@pytest.mark.parametrize("script,device,recipe", CASES)
def test_reviewed_native_contract(script, device, recipe):
    assert "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ, "Use a verified native tier context with --native"
    assert Path(sys.executable).name.lower() == "hython.exe", "Native contracts require Houdini hython"
    # Keep enough path budget for Houdini's own HIP/backup writers on Windows.
    label = Path(script).stem.removeprefix("houdini_").removeprefix("core_").removesuffix("_smoke")
    output = artifact_path("pytest-reviewed-native", artifact_label(label, limit=16) + "-" + device, "output").parent
    values = {"device": device, "output": str(output)}
    arguments = [value.format(**values) for value in recipe["arguments"]]
    (output / "case.json").write_text(json.dumps(dict(script=script, backend=device, arguments=arguments), indent=2))
    env = dict(
        os.environ,
        PYTHONPATH=os.environ.get("CYCLES_HOUDINI_TEST_DEPENDENCIES", ""),
        CYCLES_HOUDINI_TEST_ARTIFACTS=str(output / "artifacts"),
        CYCLES_HOUDINI_WORKER_LOG_DIR=str(output / "workers"),
        CYCLES_HOUDINI_EXECUTION="worker",
        CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(output / "identity.json"),
        HOUDINI_USER_PREF_DIR=str(output / "prefs/houdini__HVER__"),
    )
    env.update({key: value.format(**values) for key, value in recipe.get("environment", {}).items()})
    run_process(
        [sys.executable, str(ROOT / "tests/native_test_entry.py"), str(ROOT / "tests" / script), *arguments],
        timeout=900,
        destination=output,
        env=env,
        cwd=output,
    )
    # The adapter reports only completed assert-script execution. It does not
    # synthesize a passed renderer report or count unexecuted GUI/manual probes.
    (output / "execution.json").write_text(
        json.dumps(
            dict(
                script=script,
                backend=device,
                result="passed",
                evidence="original script assertions and verified child exit",
                arguments=arguments,
            ),
            indent=2,
        )
    )
