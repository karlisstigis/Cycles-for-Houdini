# SPDX-License-Identifier: Apache-2.0
"""Actual panel capture: camera reuse and independently specified shutter coverage."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import time

import hou
import numpy as np
from PySide6 import QtWidgets
from cycles_houdini import native_scene, registry
from cycles_houdini.panel import RenderPanel

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
hou.hipFile.load(str(artifacts_root() / "stage2/motion/native-velocity-motion.hiplc"), suppress_save_prompt=True)
hou.setFps(24)
hou.setFrame(1)
rop = hou.node("/out/cycles_native_motion")
set_rop_devices(rop, device)
rop.setParms(dict(samples=4096, allowmotionblur=1, geometry_motion="velocity", motion_samples=3, shutteroffset=0))
camera = hou.node(rop.evalParm("camera"))
camera.setParms(dict(shutter=0.5, tx=0, tz=5))
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
panel = RenderPanel()
panel.auto.setChecked(False)
panel.set_rop(rop)
panel.show()
checks = []


def render(name, expected, *, refresh_motion=False):
    old = panel.entry.session.status() if panel.entry else None
    if refresh_motion:
        assert not panel._camera_capture_allowed(), "Changed motion timing took the camera-only shortcut"
    panel.render()
    entry = panel.entry
    assert entry is not None and not entry.error, panel.info.text()
    generation = entry.session.status()["generation"]
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        status = entry.session.status()
        info = entry.session.frame_info()
        if (
            info
            and info["complete"]
            and info["generation"] == generation
            and panel._live_generation == generation
            and not panel._settings_dirty
        ):
            break
        time.sleep(0.005)
    else:
        raise TimeoutError(panel.info.text())
    assert not entry.error and status["state"] == "complete"
    frame = entry.session.frame()
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(90, 160, 4)
    measured = float(pixels[44:46, 79:81, 0].mean())
    assert abs(measured - expected) < 0.045, (name, measured, expected)
    if old:
        assert status["renderer_pid"] == old["renderer_pid"]
        if refresh_motion:
            assert status["geometry_conversions"] > old["geometry_conversions"], (name, status)
        else:
            assert status["geometry_conversions"] == old["geometry_conversions"], (name, status)
    checks.append(dict(name=name, expected_red=expected, measured_red=measured, status=status))


try:
    render("Half-frame velocity blur", 0.32)
    camera.parm("tz").set(6)
    render("Camera move retains geometry samples", 0.32)
    camera.parm("tz").set(7)
    assert panel._camera_capture_allowed(), "Ordinary camera edits lost their shortcut"
    render("Warm camera move uses camera-only capture", 0.32)
    camera.parm("shutter").set(0.25)
    render("Quarter-frame shutter refreshes geometry motion", 0.64, refresh_motion=True)
    hou.setFps(48)
    render("FPS changes resample velocity in seconds", 0.8, refresh_motion=True)
    hou.setFps(24)
    render("Restoring FPS restores the trail", 0.64, refresh_motion=True)
    rop.parm("motion_samples").set(5)
    # Velocity blur of unpacked points always uses three steps (open, centre, close), as Blender
    # does (commit 65b6114): the sample count leaves the particle's geometry and image unchanged.
    render("Five shutter samples keep the three velocity steps of points", 0.64)
    rop.parm("allowmotionblur").set(0)
    render("Disabling blur restores the static particle", 0.8, refresh_motion=True)
    rop.parm("allowmotionblur").set(1)
    render("Re-enabling blur restores velocity", 0.64, refresh_motion=True)
    source = hou.node(rop.evalParm("soppath"))
    original = source.evalParm("python")
    source.parm("python").set(original + "\ng.findPointAttrib('v').destroy()\n")
    render("Missing velocity updates the panel with static geometry", 0.8, refresh_motion=True)
    source.parm("python").set(original)
    render("Restored velocity updates the panel with blur", 0.64, refresh_motion=True)
    source.parm("python").set(original + "\n[p.setAttribValue('v', (float('nan'), 0., 0.)) for p in g.points()]\n")
    render("Malformed velocity stays visible in the panel", 0.8, refresh_motion=True)
    assert "velocity blur disabled" in panel.info.text()
    source.parm("python").set(original)
    render("Repairing velocity clears the panel warning", 0.64, refresh_motion=True)
    assert not panel.entry.session.status()["motion_warnings"]
    assert panel.entry.session.diagnostics()["session_cancel_calls"] == 0
finally:
    session = panel.entry.session if panel.entry else None
    panel.dispose()
    panel.close()
    registry.close_all()
    if session:
        assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
report = dict(
    result="passed",
    device=device,
    checks=checks,
    scope="Actual headless Qt panel capture and linear renderer images; analytic coverage, not UI response timing",
)
destination = artifacts_root() / "stage1/panel-motion-timing"
destination.mkdir(parents=True, exist_ok=True)
(destination / "houdini-panel-motion-timing-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", cases=len(checks))))
