# SPDX-License-Identifier: Apache-2.0
"""Live COP Remap resolution edits must not turn camera moves into image cooks."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore
from cycles_houdini import _native, registry
from cycles_houdini.panel import RenderPanel
from shader_bypass_preview_fixture import Fixture

f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
cop = f.geo.createNode("copnet", "navigation_texture")
cop.setParms(dict(setres=1, res1=1024, res2=1024, setprecision=1, precision=1))
color = cop.createNode("constant", "color")
color.parm("signature").set("f1")
color.parm("f1").set(0.3)
remap = cop.createNode("remap", "remap")
remap.setInput(0, color)
out = cop.createNode("null", "out")
out.setInput(0, remap)
source = "op:" + out.path()
f.texture.setParms(dict(filename=source, colorspace="data", alpha_type="ignore"))
f.output.setNamedInput("surface", f.texture, "color")
panel = RenderPanel()
panel.resize(1350, 800)
panel.set_rop(f.rop)
panel.show()
panel.auto.setChecked(True)
panel.render()
report = dict(result="running", cases=[])
destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
began = time.monotonic()
phase = "wait"
case = step = 0
last = 0
busy = False
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(25)


def counts():
    return {node.path(): node.cookCount() for node in cop.allSubChildren()}


def finish(error=None):
    timer.stop()
    panel.dispose()
    panel.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global phase, case, step, last, busy, anchor, cooks
    if busy:
        return
    busy = True
    try:
        now = time.monotonic()
        assert now - began < 150, (phase, case)
        if not panel.entry:
            return
        session = panel.entry.session
        status = session.status()
        assert not status["error"] and not panel.entry.error, (status, panel.entry.error)
        if phase == "wait":
            if status["state"] != "complete" or panel.entry.change_serial != panel.entry.applied_serial:
                return
            f.expect(session.frame(), (0.3, 0.3, 0.3) if case < 3 else (0.6, 0.6, 0.6))
            info = _native.cop_image_source_info(source)
            # Name-only ROP interests retain identity with a -1 version sentinel.
            # They must not traverse into camera parameters or recook pixels.
            assert f.camera.sessionId() not in info["nodes"], info
            if case == 3:
                finish()
                return
            assert f.camera not in panel.entry.observer.non_camera_dependencies
            assert f.camera not in panel._observer.non_camera_dependencies
            anchor = _native.cop_capture_stats(session._handle)
            cooks = counts()
            step = 0
            last = now
            phase = "move"
        elif phase == "move":
            assert _native.cop_capture_stats(session._handle)["captures"] == anchor["captures"]
            assert counts() == cooks, "Camera navigation recooked COP pixels"
            if now - last < 0.35:
                return
            last = now
            if step == 14:
                report["cases"].append(
                    dict(
                        resolution=cop.evalParm("res1"),
                        camera_edits=step,
                        before=anchor,
                        after=_native.cop_capture_stats(session._handle),
                    )
                )
                case += 1
                if case < 3:
                    editor = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
                    if editor:
                        editor.setPwd(cop)
                    cop.setCurrent(True, clear_all_selected=True)
                    cop.parmTuple("res").set((2048, 2048) if case == 1 else (1024, 1024))
                else:
                    color.parm("f1").set(0.6)
                phase = "wait"
                return
            f.camera.parm("tx").set((step % 2) * 0.05)
            f.camera.parm("ry").set((step % 2) * 0.25)
            step += 1
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
