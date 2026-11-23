# SPDX-License-Identifier: Apache-2.0
"""Reject false success when report status or native exit status disagrees."""

from support.paths import artifacts_root
import copy
import importlib.util
import json
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verification", root / "scripts/verify-verification.py")
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)
summary = dict(result="passed", tests=[dict(result="passed", exit_code=0)])
report = dict(result="passed", process_exit_code=0)
verification.validate_outcome(summary, report)
cases = []
for name, subject, key, value in (
    ("failed report", "report", "result", "failed"),
    ("missing report status", "report", "result", None),
    ("unknown native exit", "report", "process_exit_code", None),
    ("nonzero native exit", "report", "process_exit_code", 1),
    ("failed entry", "entry", "result", "failed"),
    ("unknown entry exit", "entry", "exit_code", None),
    ("nonzero entry exit", "entry", "exit_code", 1),
):
    changed = copy.deepcopy(summary)
    changed_report = copy.deepcopy(report)
    target = changed_report if subject == "report" else changed["tests"][0]
    target[key] = value
    try:
        verification.validate_outcome(changed, changed_report)
    except RuntimeError:
        cases.append(name)
    else:
        raise AssertionError("False success was accepted: " + name)
# Provenance verification remains available for honestly reported failed runs.
verification.validate_outcome(dict(result="failed", tests=[dict(result="failed", exit_code=1)]), dict(result="failed"))
with tempfile.TemporaryDirectory() as build:
    expected = Path(build).resolve()
    assert verification.recorded_build_directory(dict(build_directory=build)) == expected
    assert verification.recorded_build_directory(dict(runtime_identity=dict(build_directory=build))) == expected
    assert (
        verification.recorded_build_directory(dict(build_directory=build, runtime_identity=dict(build_directory=build)))
        == expected
    )
    for name, context in (
        ("missing recorded build", {}),
        ("relative recorded build", dict(build_directory="build/not-absolute")),
        (
            "conflicting recorded builds",
            dict(build_directory=build, runtime_identity=dict(build_directory=str(expected.parent))),
        ),
        ("missing recorded build directory", dict(build_directory=str(expected / "absent"))),
    ):
        try:
            verification.recorded_build_directory(context)
        except RuntimeError:
            cases.append(name)
        else:
            raise AssertionError("Unrecorded build context was accepted: " + name)
destination = artifacts_root() / "stage0/verification-outcome"
destination.mkdir(parents=True, exist_ok=True)
(destination / "report.json").write_text(json.dumps(dict(result="passed", rejected=cases), indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", rejected=len(cases))))
