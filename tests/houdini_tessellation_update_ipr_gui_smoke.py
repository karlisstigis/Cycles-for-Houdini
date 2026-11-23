# SPDX-License-Identifier: Apache-2.0
"""Actual viewer icons, uncached startup, held navigation and one-shot refresh."""

import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import object_properties, registry
from cycles_houdini.panel import RenderPanel
from shader_bypass_preview_fixture import Fixture

f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
coords = f.builder.createNode("cycles::texture_coordinate::1", "gradient")
f.emission.setNamedInput("color", coords, "generated")
f.output.setNamedInput("surface", f.emission, "emission")
object_properties.install(f.geo)
f.geo.setParms(dict(cycles_subdivision_override=1, cycles_subdivision_type="linear", cycles_subdivision_adaptive=1))
assert f.rop.evalParm("tessellation_update") == 1
f.rop.setParms(
    dict(tessellation_update=0, ipr_dicing_rate=2, max_subdivisions=8, offscreen_dicing_scale=1, ipr_samples=1048576)
)
p = RenderPanel()
p.resize(1350, 800)
p.set_rop(f.rop)
p.show()
p.render()
dest = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
report = dict(result="running", events=[])
began = time.monotonic()
phase = 0
generation = 0
baseline = None
refreshed = None
pid = None
busy = False
settle_at = 0
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(25)


def finish(error=None):
    timer.stop()
    p.dispose()
    p.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global phase, generation, baseline, refreshed, pid, busy, settle_at
    if busy:
        return
    busy = True
    try:
        assert time.monotonic() - began < 120, phase
        if not p.entry:
            return
        entry = p.entry
        session = entry.session
        status = session.status()
        assert not entry.error and not status["error"], (phase, entry.error, status)
        frame = session.frame_info()
        if not frame or frame["generation"] != status["generation"]:
            return
        if phase in (4, 6):
            expected = baseline if phase == 4 else refreshed
            assert status["subdivision_tessellations"] == expected["subdivision_tessellations"], status
            assert not entry.camera_refinement_pending
            if time.monotonic() < settle_at:
                return
            generation = status["generation"]
            if phase == 4:
                QtTest.QTest.mouseClick(p.tessellation_refresh, QtCore.Qt.LeftButton)
                phase = 5
            else:
                QtTest.QTest.mouseClick(p.tessellation_update, QtCore.Qt.LeftButton)
                phase = 7
            return
        if (
            status["generation"] <= generation
            or entry.change_serial != entry.applied_serial
            or status.get("capture_pending")
            or status.get("pending_update")
        ):
            return
        if phase == 7 and not p.tessellation_update.isChecked():
            return
        if phase == 8 and p.tessellation_update.isChecked():
            return
        if pid is None:
            pid = status["renderer_pid"]
        assert pid == status["renderer_pid"], "Tessellation action recreated the worker"
        values = np.frombuffer(session.frame()["planes"]["combined"]["pixels"], np.float32)
        assert np.isfinite(values).all()
        report["events"].append(
            dict(
                phase=phase,
                generation=status["generation"],
                tessellations=status["subdivision_tessellations"],
                triangles=status["subdivision_triangles"],
                renderer_pid=pid,
            )
        )
        generation = status["generation"]
        if phase == 0:
            assert status["subdivision_tessellations"] == 0 and status["subdivision_triangles"] == 2, status
            assert p.tessellation_update.isVisible() and not p.tessellation_update.isChecked()
            assert p.tessellation_update.text() == p.tessellation_refresh.text() == ""
            assert not p.tessellation_update.icon().isNull() and not p.tessellation_refresh.icon().isNull()
            wrapper = p.tessellation_update.parentWidget()
            assert wrapper is p.tessellation_refresh.parentWidget()
            assert p.tessellation_update.x() < p.tessellation_refresh.x()
            p.auto.setChecked(False)
            QtTest.QTest.mouseClick(p.tessellation_refresh, QtCore.Qt.LeftButton)
            phase = 1
        elif phase == 1:
            assert not p.tessellation_update.isChecked() and not f.rop.evalParm("tessellation_update")
            assert status["subdivision_tessellations"] == 1 and status["subdivision_triangles"] > 2
            baseline = status
            p.auto.setChecked(True)
            f.camera.setParms(dict(tx=0.5, orthowidth=8))
            phase = 2
        elif phase == 2:
            assert status["subdivision_tessellations"] == baseline["subdivision_tessellations"]
            f.geo.parm("sx").set(0.5)
            phase = 3
        elif phase == 3:
            assert status["subdivision_tessellations"] == baseline["subdivision_tessellations"]
            assert status["subdivision_triangles"] == baseline["subdivision_triangles"]
            p.grab().save(str(dest.with_suffix(".off.png")))
            p.tessellation_update.parentWidget().grab().save(str(dest.with_suffix(".controls-off.png")))
            settle_at = time.monotonic() + 0.6
            phase = 4
        elif phase == 5:
            assert not p.tessellation_update.isChecked()
            assert status["subdivision_tessellations"] == baseline["subdivision_tessellations"] + 1
            assert status["subdivision_triangles"] < baseline["subdivision_triangles"]
            refreshed = status
            settle_at = time.monotonic() + 0.4
            phase = 6
        elif phase == 7:
            assert f.rop.evalParm("tessellation_update") == 1
            assert status["subdivision_tessellations"] > refreshed["subdivision_tessellations"]
            p.grab().save(str(dest.with_suffix(".on.png")))
            p.tessellation_update.parentWidget().grab().save(str(dest.with_suffix(".controls-on.png")))
            refreshed = status
            f.rop.parm("tessellation_update").set(0)
            phase = 8
        elif phase == 8:
            assert status["subdivision_tessellations"] == refreshed["subdivision_tessellations"]
            assert status["geometry_conversions"] == baseline["geometry_conversions"]
            report.update(one_shot_refresh_with_auto_off=True, shared_wrapper=True, uncached_original_mesh=True)
            finish()
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
