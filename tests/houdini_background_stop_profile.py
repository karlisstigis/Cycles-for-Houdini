# SPDX-License-Identifier: Apache-2.0
"""Profile the unchanged background final lifecycle, including the actual engine cancel boundary."""

from support.paths import artifacts_root

import json
import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from cycles_houdini import background_render as bg

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/background-stop-profile" / str(time.time_ns())
destination.mkdir(parents=True)
hooks = Path(__file__).with_name("background_stop_profile_hooks.py").read_text(encoding="utf-8")
hou.appendSessionModuleSource(hooks.replace("__PROFILE_DIRECTORY__", repr(str(destination))))
report = dict(result="running", scope=__doc__)
isolated = os.environ.get("CYCLES_HOUDINI_STOP_PROFILE_COLD") == "1"
original_launch = bg.launch
cache_root = destination / "isolated-gpu-cache"
if isolated:
    # Use new private caches rather than deleting or mutating the artist's
    # caches. Only the long cancel/interrupt jobs receive this environment.
    for name in ("cuda", "optix"):
        (cache_root / name).mkdir(parents=True)

    def isolated_launch(rop):
        if rop.evalParm("time_limit") != 30:
            return original_launch(rop)
        values = dict(CUDA_CACHE_PATH=str(cache_root / "cuda"), OPTIX_CACHE_PATH=str(cache_root / "optix"))
        previous = {name: hou.getenv(name) for name in values}
        try:
            for name, value in values.items():
                hou.putenv(name, value)
            return original_launch(rop)
        finally:
            for name, value in previous.items():
                hou.putenv(name, value) if value is not None else hou.unsetenv(name)

    bg.launch = isolated_launch
try:
    fixture = runpy.run_path(str(Path(__file__).with_name("houdini_background_final_smoke.py")), run_name="__main__")
    baseline = json.loads((artifacts_root() / "stage3/background-final/report.json").read_text())
    cancelled = baseline["checks"][3]["status"]
    trace = [json.loads(line) for line in (destination / f"session-{cancelled['pid']}.jsonl").read_text().splitlines()]
    detected = next(row for row in trace if row["event"] == "stop_detected")
    cancels = [row for row in trace if row["event"] == "cancel_end"]
    assert cancels and cancelled["state"] == "cancelled" and baseline["result"] == "passed"
    report.update(
        result="passed",
        checks=len(baseline["checks"]),
        device=baseline["device"],
        lifecycle=baseline,
        trace=trace,
        notice_seconds=(detected["wall_ns"] - detected["requested_wall_ns"]) / 1e9,
        cancel_seconds=[row["seconds"] for row in cancels],
        close_seconds=[row["seconds"] for row in trace if row["event"] == "close_end"],
        acknowledgement_seconds=(cancelled["finished"] * 1e9 - detected["requested_wall_ns"]) / 1e9,
    )
    if isolated:
        cache_files = [
            dict(path=str(path.relative_to(cache_root)), bytes=path.stat().st_size)
            for path in cache_root.rglob("*")
            if path.is_file()
        ]
        if baseline["device"] != "CPU":
            assert any(row["path"].startswith("optix") for row in cache_files), cache_files
        repeats = int(os.environ.get("CYCLES_HOUDINI_STOP_PROFILE_REPEATS", "1"))
        assert 1 <= repeats <= 32
        observations = []
        for number in range(repeats):
            rop = fixture["rop"]
            rop.setParms(
                dict(samples=1048576, time_limit=30, picture=str(destination / f"warm-cancel-{number}.$F4.exr"))
            )
            warm = bg.launch(rop)
            fixture["active_render"](warm)
            warm.stop()
            finished = fixture["wait"](warm)
            assert finished["state"] == "cancelled" and finished["completed_frames"] == 0
            warm_trace = [
                json.loads(line) for line in (destination / f"session-{finished['pid']}.jsonl").read_text().splitlines()
            ]
            warm_detected = next(row for row in warm_trace if row["event"] == "stop_detected")
            observations.append(
                dict(
                    trace=warm_trace,
                    notice_seconds=(warm_detected["wall_ns"] - warm_detected["requested_wall_ns"]) / 1e9,
                    cancel_seconds=[row["seconds"] for row in warm_trace if row["event"] == "cancel_end"],
                    acknowledgement_seconds=(finished["finished"] * 1e9 - warm_detected["requested_wall_ns"]) / 1e9,
                )
            )
        report.update(
            isolated_disk_caches=True,
            cache_files=cache_files,
            warm_observations=observations,
            warm_trace=warm_trace,
            warm_notice_seconds=observations[-1]["notice_seconds"],
            warm_cancel_seconds=observations[-1]["cancel_seconds"],
            warm_acknowledgement_seconds=observations[-1]["acknowledgement_seconds"],
        )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    bg.launch = original_launch
    for job in bg.active_jobs():
        if job.is_running():
            job.stop()
            try:
                job.wait(timeout=15)
            except Exception:
                job.terminate()
                job.wait(timeout=15)
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        {
            key: report.get(key)
            for key in ("result", "notice_seconds", "cancel_seconds", "close_seconds", "acknowledgement_seconds")
        }
    )
)
