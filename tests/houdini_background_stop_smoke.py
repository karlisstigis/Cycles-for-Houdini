# SPDX-License-Identifier: Apache-2.0
"""Regression budget for the reproduced quick-cancel/session-join race, not general scene latency."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import runpy
import sys
from support.devices import fixture_device
import traceback

device = fixture_device(sys.argv)
os.environ["CYCLES_HOUDINI_STOP_PROFILE_COLD"] = "1"
os.environ["CYCLES_HOUDINI_STOP_PROFILE_DETAIL"] = "1"
os.environ["CYCLES_HOUDINI_STOP_PROFILE_REPEATS"] = "1" if device == "CPU" else "5"
runpy.run_path(str(Path(__file__).with_name("houdini_background_stop_profile.py")), run_name="__main__")
path = artifacts_root() / "stage3/background-stop-profile/report.json"
report = json.loads(path.read_text())
durations = [report["acknowledgement_seconds"]] + [
    value["acknowledgement_seconds"] for value in report["warm_observations"]
]
try:
    assert max(durations) < 2.0, durations
    for sequence in [report["trace"]] + [value["trace"] for value in report["warm_observations"]]:
        timings = [row["diagnostics"]["engine_update_seconds"] for row in sequence if row["event"] == "cancel_end"]
        assert timings and max(value["cancel/session_wait"] for value in timings) < 1.0, timings
    report.update(scope=__doc__, cancellation_regression_budget_seconds=2, measured_acknowledgement_seconds=durations)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", stop_count=len(durations), maximum_seconds=max(durations))))
