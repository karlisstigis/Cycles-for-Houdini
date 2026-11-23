# SPDX-License-Identifier: Apache-2.0
"""Live GPU viewer: linked zoom/Fit/resize, controls and bounded event-loop work."""

import json
import math
import os
from pathlib import Path
import time
import traceback
from fractions import Fraction
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, output
from cycles_houdini.panel import RenderPanel
from shader_bypass_preview_fixture import Fixture

f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
f.output.setNamedInput("surface", f.emission, "emission")
f.rop.setParms(dict(resx=640, resy=360, ipr_resolution_percent=100, ipr_samples=1048576))
p = RenderPanel()
p.resize(1400, 800)
p.set_rop(f.rop)
p.canvas.setFixedSize(560, 315)
p.show()
p.render()
dest = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
started = time.monotonic()
report = dict(result="running", events=[], edited_during_accumulation=False, phase_ui_gaps={})
phase = 0
session = None
generation = 0
since = started
last = None
gaps = []
busy = False
rect = None
region_reference = None
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(20)


def finish(error=None):
    timer.stop()
    p.dispose()
    p.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error, max_ui_gap=max(gaps) if gaps else 0)
    dest.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def ready(size):
    if not (
        p.entry
        and p.canvas.full_size == size
        and p.entry.config[1:3] == size
        and p.entry.change_serial == p.entry.applied_serial
        and not p._pending
        and not p._render_queued
    ):
        return False
    if region_reference:
        crop, original = region_reference
        x, y, w, h = crop
        rw, rh = original
        left = math.floor(Fraction(x * size[0], rw))
        bottom = math.floor(Fraction(y * size[1], rh))
        expected = (
            left,
            bottom,
            math.ceil(Fraction((x + w) * size[0], rw)) - left,
            math.ceil(Fraction((y + h) * size[1], rh)) - bottom,
        )
        assert p.entry.crop_override == expected, (p.entry.crop_override, expected)
        info = p.entry.session.frame_info()
        if not info or tuple(info[k] for k in ("x", "y", "width", "height")) != expected:
            return False
    return True


def tick():
    global phase, session, generation, since, last, busy, rect, region_reference
    if busy:
        return
    busy = True
    try:
        now = time.monotonic()
        assert now - started < 120, (phase, p.info.text())
        if last is not None:
            gap = now - last
            gaps.append(gap)
            key = str(phase)
            report["phase_ui_gaps"][key] = max(gap, report["phase_ui_gaps"].get(key, 0))
        last = now
        if not p.entry:
            return
        status = p.entry.session.status()
        assert not p.entry.error and not status["error"], (p.entry.error, status)
        report["edited_during_accumulation"] |= status["state"] == "rendering"
        if phase == 0 and ready((640, 360)):
            assert p.channel_wrapper.height() == p.resolution_group.height()
            report["wrapper_heights"] = [p.channel_wrapper.height(), p.resolution_group.height()]
            p.region.click()
            area = p.canvas.image_rect()
            start = QtCore.QPoint(round(area.left() + area.width() * 0.2), round(area.top() + area.height() * 0.25))
            end = QtCore.QPoint(round(area.left() + area.width() * 0.65), round(area.top() + area.height() * 0.7))
            QtTest.QTest.mousePress(p.canvas, QtCore.Qt.LeftButton, pos=start)
            QtTest.QTest.mouseMove(p.canvas, end)
            QtTest.QTest.mouseRelease(p.canvas, QtCore.Qt.LeftButton, pos=end)
            assert p.entry.crop_override is not None
            region_reference = (p.entry.crop_override, p.entry.config[1:3])
            report["region_reference"] = region_reference
            phase = 0.5
        elif phase == 0.5 and ready((640, 360)):
            p.resolution_link.click()
            phase = 1
        elif phase == 1 and ready(output.scaled_dimensions((640, 360), p.canvas.zoom_percent())):
            assert not p.keep_image_size.isEnabled() and p.keep_image_size.isChecked()
            report["events"].append(dict(action="link Fit", size=list(p.canvas.full_size)))
            p.canvas.set_zoom(50)
            phase = 2
        elif phase == 2 and ready((320, 180)):
            p.canvas.set_zoom(87.5)
            phase = 3
        elif phase == 3 and ready((560, 315)):
            p.resolution_group.grab().save(str(dest.with_name("linked-controls.png")))
            p.canvas.set_zoom(176)
            phase = 4
        elif phase == 4 and ready((1126, 633)):
            report["events"].append(dict(action="zoom 50/87.5/176", size=list(p.canvas.full_size)))
            report["events"].append(
                dict(
                    action="Mouse-selected region preserved in worker frame after every zoom",
                    crop=p.entry.crop_override,
                )
            )
            session = p.entry.session
            generation = status["generation"]
            since = now
            phase = 5
        elif phase == 5 and now - since > 1:
            assert p.entry.session is session and status["generation"] == generation
            p.canvas.fit_image()
            phase = 6
        elif phase == 6 and ready(output.scaled_dimensions((640, 360), p.canvas.zoom_percent())):
            p.canvas.setFixedSize(480, 270)
            phase = 7
        elif phase == 7 and ready(output.scaled_dimensions((640, 360), p.canvas.zoom_percent())):
            report["events"].append(dict(action="Fit/resize stable", size=list(p.canvas.full_size)))
            p.actual_pixels.click()
            phase = 8
        elif phase == 8 and ready((640, 360)):
            session = p.entry.session
            p.cancel()
            p.canvas.set_zoom(125)
            since = now
            phase = 9
        elif phase == 9 and now - since > 0.6:
            assert p._stopped and p.entry.session is session
            assert math.isclose(f.rop.evalParm("ipr_resolution_percent"), 125, abs_tol=1e-7)
            p.render()
            phase = 10
        elif phase == 10 and ready((800, 450)):
            rect = p.canvas.image_rect()
            session = p.entry.session
            generation = status["generation"]
            p.resolution_link.click()
            since = now
            phase = 11
        elif phase == 11 and now - since > 0.6:
            assert p.preview_resolution.isEnabled()
            assert p.entry.session is session and status["generation"] == generation
            assert abs(p.canvas.image_rect().width() - rect.width()) < 1e-6
            assert report["edited_during_accumulation"]
            report["events"].append(
                dict(action="1:1, Stop/Play and unlink preserve control semantics", size=list(p.canvas.full_size))
            )
            p.grab().save(str(dest.with_name("linked-viewer.png")))
            finish()
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
