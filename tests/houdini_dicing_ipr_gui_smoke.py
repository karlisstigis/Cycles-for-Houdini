# SPDX-License-Identifier: Apache-2.0
"""Edit dicing cameras/policy during live GPU accumulation and repair failures."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore
from cycles_houdini import native_scene, object_properties, registry
from cycles_houdini.panel import RenderPanel
from shader_bypass_preview_fixture import Fixture

f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
coords = f.builder.createNode("cycles::texture_coordinate::1", "gradient")
f.emission.setNamedInput("color", coords, "generated")
f.output.setNamedInput("surface", f.emission, "emission")
object_properties.install(f.geo)
f.geo.setParms(dict(cycles_subdivision_override=1, cycles_subdivision_type="linear", cycles_subdivision_adaptive=1))
dicing = f.scene.createNode("cam", "fixed_dicing")
dicing.setParms(dict(tz=5, projection=1, orthowidth=4))
f.rop.setParms(dict(dicing_camera=dicing.path(), ipr_dicing_rate=4, max_subdivisions=8, ipr_samples=1048576))
panel = RenderPanel()
panel.resize(1350, 800)
panel.set_rop(f.rop)
panel.show()
panel.auto.setChecked(True)
panel.render()
report = dict(result="running", events=[], edited_during_accumulation=False)
destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
began = time.monotonic()
phase = 0
generation = 0
busy = False
baseline = None
pid = None
image = None
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(25)


def pixels(session):
    frame = session.frame()
    plane = frame["planes"]["combined"]
    values = np.frombuffer(plane["pixels"], np.float32).reshape(64, 64, 4).copy()
    assert np.isfinite(values).all()
    return values


def finish(error=None):
    timer.stop()
    panel.dispose()
    panel.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global phase, generation, busy, baseline, pid, image
    if busy:
        return
    busy = True
    try:
        assert time.monotonic() - began < 150, phase
        if not panel.entry:
            return
        entry = panel.entry
        session = entry.session
        status = session.status()
        if phase in (4, 7):
            error = entry.error or status["error"]
            if not error:
                return
            assert "Dicing Camera" in error if phase == 4 else "Subdivision triangle budget" in error, error
            assert status["renderer_pid"] == pid
            report["events"].append(dict(phase=phase, error=error, renderer_pid=pid))
            if phase == 4:
                f.rop.parm("dicing_camera").set(dicing.path())
            else:
                f.rop.parm("limit_subdivision").set(0)
            generation = status["generation"]
            phase += 1
            return
        if entry.error or status["error"]:
            # A repaired source may still be awaiting its new capture.
            if phase in (5, 8):
                return
            raise AssertionError((phase, entry.error, status))
        frame = session.frame_info()
        if (
            not frame
            or frame["generation"] != status["generation"]
            or status["generation"] <= generation
            or entry.change_serial != entry.applied_serial
            or status.get("capture_pending")
            or status.get("pending_update")
        ):
            return
        if status["renderer_pid"]:
            if pid is None:
                pid = status["renderer_pid"]
            elif phase == 8:
                # A fatal tessellation error recreates the worker on repair.
                # Capture errors above must retain it; renderer errors need not.
                pid = status["renderer_pid"]
            else:
                assert status["renderer_pid"] == pid
        report["edited_during_accumulation"] |= status["state"] == "rendering"
        current = pixels(session)
        report["events"].append(
            dict(
                phase=phase,
                generation=status["generation"],
                state=status["state"],
                tessellations=status["subdivision_tessellations"],
                triangles=status["subdivision_triangles"],
                renderer_pid=pid,
            )
        )
        generation = status["generation"]
        if phase == 0:
            assert not entry.pixel_subdivision, "A fixed dicing camera must not follow render-camera navigation"
            baseline = status
            image = current
            f.camera.parm("tx").set(0.4)
            phase = 1
        elif phase == 1:
            assert status["subdivision_tessellations"] == baseline["subdivision_tessellations"]
            assert status["geometry_conversions"] == baseline["geometry_conversions"]
            assert np.max(np.abs(current - image)) > 0.01, "Render camera did not change the image"
            image = current
            baseline = status
            dicing.parm("orthowidth").set(8)
            phase = 2
        elif phase == 2:
            assert status["subdivision_triangles"] < baseline["subdivision_triangles"]
            # Independent accumulating frames have different subpixel samples.
            # Compare the smooth interior mean, excluding silhouette coverage.
            assert np.allclose(
                current[16:48, 16:48].mean((0, 1)), image[16:48, 16:48].mean((0, 1)), atol=0.002, rtol=0
            ), "Dicing camera changed render-camera rays"
            assert status["geometry_conversions"] == baseline["geometry_conversions"]
            baseline = status
            f.rop.parm("offscreen_dicing_scale").set(1)
            phase = 3
        elif phase == 3:
            assert status["subdivision_tessellations"] > baseline["subdivision_tessellations"]
            assert np.allclose(current[16:48, 16:48].mean((0, 1)), image[16:48, 16:48].mean((0, 1)), atol=0.002, rtol=0)
            f.rop.parm("dicing_camera").set("/obj/missing_dicing_camera")
            phase = 4
        elif phase == 5:
            assert not status["source_diagnostics"]
            baseline = status
            f.rop.parm("dicing_camera").set("")
            phase = 6
        elif phase == 6:
            assert entry.pixel_subdivision, "An empty dicing selector must follow the render camera"
            f.rop.setParms(dict(limit_subdivision=1, subdivision_limit=1))
            phase = 7
        elif phase == 8:
            assert not status["source_diagnostics"]
            assert report["edited_during_accumulation"]
            panel.cancel()
            phase = 9
            QtCore.QTimer.singleShot(100, panel.render)
        elif phase == 9:
            assert not status["error"]
            finish()
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
