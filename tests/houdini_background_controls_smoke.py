# SPDX-License-Identifier: Apache-2.0
"""Stop, Force Stop and unexpected exit with a million-triangle shared prototype."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
from support.devices import fixture_device
import time
import traceback

import hou
from cycles_houdini import background_render as bg, final_render
from packed_deformation_fixture import Fixture

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/background-controls" / str(time.time_ns())
destination.mkdir(parents=True)
hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(destination / "jobs"))
device = fixture_device(sys.argv)
report = dict(result="running", scope=__doc__, device=device, prototype_triangles=1036800, instances=16, events=[])
owned = []


def wait(job, predicate, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = job.status()
        if predicate(value):
            return value
        assert value["state"] not in bg._terminal, value
        time.sleep(0.02)
    raise TimeoutError(job.status())


try:
    f = Fixture(device)
    f.motion.parm("test_rows").set(721)
    f.targets.setParms(dict(rows=4, cols=4))
    f.targets.parmTuple("size").set((6, 6))
    rop = f.rop(f.nested)
    rop.setParms(dict(samples=1048576, time_limit=30, resx=320, resy=180, trange=0, picture="$HIP/controls.$F4.exr"))
    hou.hipFile.save(str(destination / "artist.hiplc"), save_to_recent_files=False)
    for mode in ("cooperative", "forced", "unexpected"):
        job = bg.launch(rop)
        owned.append(job)
        active = wait(
            job,
            lambda value: value["state"] == "rendering" and value.get("generation") and value.get("progress", 0) > 0,
        )
        began = time.monotonic()
        if mode == "cooperative":
            job.stop()
            immediate = job.status()
            assert immediate["state"] in ("stopping", "cancelled"), immediate
            stamp = (job.directory / "stop").stat().st_mtime_ns
            job.stop()
            assert (job.directory / "stop").stat().st_mtime_ns == stamp
        elif mode == "forced":
            job.force_stop()
        else:
            job.terminate()
        terminal = wait(job, lambda value: value["state"] in bg._terminal)
        job.wait(timeout=15)
        assert terminal["completed_frames"] == 0 and not list(destination.glob("controls.*.exr")), terminal
        if mode == "unexpected":
            assert terminal["state"] == "failed" and not terminal["forced"], terminal
        else:
            assert terminal["state"] == "cancelled", terminal
        if mode == "forced":
            assert terminal["forced"], terminal
        if mode == "cooperative":
            assert terminal["stop_requested_wall_ns"] <= terminal["stop_observed_wall_ns"]
            assert terminal["cancel_seconds"] < 2, terminal
        assert not job.has_process_handle
        report["events"].append(
            dict(mode=mode, active=active, terminal=terminal, acknowledgement_seconds=time.monotonic() - began)
        )
    assert not final_render.active_sequences() and rop.cachedUserData(final_render._summary_key) is None
    report.update(result="passed", no_foreground_session=True)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    for job in owned:
        if job.is_running():
            job.terminate()
            job.wait(timeout=15)
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], events=len(report["events"]), report=str(destination / "report.json"))))
