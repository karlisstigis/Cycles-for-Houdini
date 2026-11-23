# SPDX-License-Identifier: Apache-2.0
"""Viewer time advances, freezes on completion/Stop and restarts on edits.

The status line counts from the update's trigger (loading + render); the
renderer's pure render time stays unchanged in the tooltip ("Render ... s").
"""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
from PySide6 import QtWidgets
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel
from cycles_houdini.viewer_timing import duration, frame_seconds, summary, text

assert duration(0) == "00:00.00" and duration(1.234) == "00:01.23"
assert duration(59.999) == "01:00.00" and duration(3661.25) == "01:01:01.25"
assert duration(-1) == duration(float("nan")) == "00:00.00"
assert text(2, 1) == "Time: 00:02.00 · Last: 00:01.00"
assert (
    frame_seconds(dict(state="completed", frame_elapsed_seconds=3, frame_started_monotonic=time.monotonic() - 30)) == 3
)
assert "Last:" in summary("Rendering · 50.0% · " + text(2, 1) + " · Frame 4 (2/3) · Synchronizing scene")

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
dest = artifacts_root() / "stage3/viewer-timing"
dest.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "timing_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "timing_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "timing_render")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=camera.path(),
        resx=96,
        resy=64,
        ipr_samples=1048576,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        render_threads=2,
        allowmotionblur=0,
    )
)
p = RenderPanel()
p.resize(1400, 700)
p.set_rop(rop)
p.show()
p.render()
checks = []


def shown():
    """Seconds shown in the painted status summary."""
    match = re.search(r"Time: (?:(\d+):)?(\d+):(\d+\.\d+)", summary(p.info.text()))
    assert match, p.info.text()
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def pure_render_in_tooltip(elapsed):
    assert f"Render {elapsed:.2f} s" in p.info.toolTip(), p.info.toolTip()
    # The shown total is loading plus exactly this render time.
    loading = re.search(r"Loading (\d+\.\d+) s", p.info.toolTip())
    assert loading, p.info.toolTip()
    assert abs(shown() - (float(loading.group(1)) + elapsed)) <= 0.02, (shown(), loading.group(1), elapsed)


def wait(predicate):
    end = time.monotonic() + 30
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        status = p.entry.session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if predicate(status):
            return status
        time.sleep(0.005)
    raise TimeoutError(p.info.text())


try:
    status = wait(lambda s: s["state"] == "rendering" and s["elapsed_seconds"] > 0.2)
    first = status["elapsed_seconds"]
    generation = status["generation"]
    wait(lambda s: s["elapsed_seconds"] > first + 0.15)
    assert "Time:" in summary(p.info.text()) and "Last:" not in summary(p.info.text())
    checks.append("IPR clock advances during actual sampling and is included in the painted status summary")
    rop.parm("ipr_samples").set(4)
    p.render()
    complete = wait(lambda s: s["generation"] > generation and s["state"] == "complete" and not p._pending)
    elapsed = complete["elapsed_seconds"]
    generation = complete["generation"]
    assert elapsed > 0 and shown() >= elapsed - 0.01
    pure_render_in_tooltip(elapsed)
    completed_shown = shown()
    p.exposure.setValue(1)
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    status = p.entry.session.status()
    assert status["generation"] == generation and status["elapsed_seconds"] == elapsed
    assert shown() == completed_shown
    pure_render_in_tooltip(elapsed)
    checks.append("Completed time (loading + render) stays fixed while idle and during display-only exposure edits")
    p.grab().save(str(dest / "completed-ipr.png"))
    rop.parm("ipr_samples").set(1048576)
    p.render()
    wait(lambda s: s["generation"] > generation and s["state"] == "rendering" and s["elapsed_seconds"] > 0.15)
    p.stop_button.click()
    cancelled = wait(lambda s: s["state"] == "cancelled")
    stopped = cancelled["elapsed_seconds"]
    end = time.monotonic() + 0.25
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    assert p.entry.session.status()["elapsed_seconds"] == stopped
    stopped_shown = shown()
    assert stopped_shown >= stopped - 0.01
    pure_render_in_tooltip(stopped)
    checks.append("New render resets its elapsed time; Stop freezes the cancelled render duration")
finally:
    p.dispose()
    p.close()
    registry.close_all()
    app.processEvents()
report = dict(
    result="passed",
    device=device,
    completed_seconds=elapsed,
    completed_shown_seconds=completed_shown,
    cancelled_seconds=stopped,
    cancelled_shown_seconds=stopped_shown,
    checks=checks,
)
(dest / "houdini-viewer-timing-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
