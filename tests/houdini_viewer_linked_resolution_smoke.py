# SPDX-License-Identifier: Apache-2.0
"""Linked display footprint, fractional buffers, stable Fit and HIP-local controls."""

import json
import math
import os
from pathlib import Path
import sys
import time
from fractions import Fraction

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
import OpenImageIO as oiio
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import native_scene, output, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
dest = Path(os.environ["CH_LINKED_REPORT"])
dest.parent.mkdir(parents=True, exist_ok=True)
device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
geo = hou.node("/obj").createNode("geo", "linked_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
cam = hou.node("/obj").createNode("cam", "linked_camera")
cam.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "linked_resolution")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=cam.path(),
        resx=640,
        resy=360,
        resolution_percent=75,
        ipr_resolution_percent=100,
        samples=4,
        ipr_samples=1048576,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        render_threads=2,
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        allowmotionblur=0,
        picture=str(dest.parent / "disk.exr"),
    )
)
panel = RenderPanel()
panel.resize(1400, 800)
panel.set_rop(rop)
panel.canvas.setFixedSize(560, 315)
panel.show()
panel.render()
report = dict(result="running", checks=[], device=device)


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        panel.poll()
        time.sleep(0.005)


def wait(size):
    end = time.monotonic() + 40
    while time.monotonic() < end:
        app.processEvents()
        panel.poll()
        if panel.entry:
            status = panel.entry.session.status()
            assert not panel.entry.error and not status["error"], (panel.entry.error, status)
            if (
                panel.canvas.full_size == size
                and panel.entry.config[1:3] == size
                and not panel._pending
                and not panel._render_queued
                and not panel._in_render
                and panel.entry.change_serial == panel.entry.applied_serial
                and not status.get("capture_pending")
            ):
                return
        time.sleep(0.005)
    raise TimeoutError((size, panel.canvas.full_size, panel.info.text()))


def record(text):
    report["checks"].append(text)
    dest.write_text(json.dumps(report, indent=2))


def zoom(percent):
    panel.canvas.set_zoom(percent)
    wait(output.scaled_dimensions((640, 360), percent))
    assert math.isclose(rop.evalParm("ipr_resolution_percent"), percent, abs_tol=1e-7)


try:
    wait((640, 360))
    widgets = [
        panel.resolution_group.layout().itemAt(i).widget() for i in range(panel.resolution_group.layout().count())
    ]
    assert widgets == [
        panel.keep_image_size,
        panel.preview_resolution,
        panel.resolution_link,
        panel.zoom_menu,
        panel.actual_pixels,
        panel.fit_button,
    ]
    assert panel.keep_image_size.property("cycles_transport_start") and panel.fit_button.property(
        "cycles_transport_end"
    )
    assert not panel.actual_pixels.property("cycles_transport_start")
    record("One continuous wrapper, size lock at left and Fit at right; no internal corner endpoints")
    panel.resolution_link.click()
    spin(0.05)
    expected = output.scaled_dimensions((640, 360), panel.canvas.zoom_percent())
    wait(expected)
    assert panel.keep_image_size.isChecked() and not panel.keep_image_size.isEnabled()
    assert not panel.preview_resolution.isEnabled()
    for percent in (50.0, 87.5, 176.0):
        zoom(percent)
    record("Linked zoom 50%, 87.5% and 176% produces matching full-frame fractional-resolution buffers")
    panel.canvas.fit_image()
    spin(0.05)
    expected = output.scaled_dimensions((640, 360), panel.canvas.zoom_percent())
    wait(expected)
    panel.canvas.setFixedSize(640, 360)
    spin(0.05)
    expected = output.scaled_dimensions((640, 360), panel.canvas.zoom_percent())
    wait(expected)
    session = panel.entry.session
    generation = session.status()["generation"]
    percent = rop.evalParm("ipr_resolution_percent")
    spin(1.0)
    assert panel.entry.session is session and session.status()["generation"] == generation
    assert rop.evalParm("ipr_resolution_percent") == percent
    record("Fit and widget resizing settle once; continuing frame delivery causes no resolution feedback/restarts")
    size = panel.canvas.full_size
    selection = (size[0] // 5, size[1] // 7, size[0] // 3, size[1] // 3)
    panel.set_region(selection)
    for percent in (50, 87.5, 176, 50, 87.5):
        zoom(percent)
        x, y, w, h = selection
        rw, rh = size
        fw, fh = panel.entry.config[1:3]
        left = math.floor(Fraction(x * fw, rw))
        bottom = math.floor(Fraction(y * fh, rh))
        expected = (
            left,
            bottom,
            math.ceil(Fraction((x + w) * fw, rw)) - left,
            math.ceil(Fraction((y + h) * fh, rh)) - bottom,
        )
        assert panel.entry.crop_override == expected, (panel.entry.crop_override, expected)
        frame = panel.entry.session.frame_info()
        assert tuple(frame[k] for k in ("x", "y", "width", "height")) == expected, frame
        assert panel.region.isChecked()
    panel.reset_region()
    spin(0.2)
    assert panel.entry.crop_override is None and panel.entry.crop_reference is None and not panel.region.isChecked()
    record(
        "Region scales proportionally through fractional zoom changes without drift; reset clears its saved reference"
    )
    zoom(87.5)
    session = panel.entry.session
    percent = rop.evalParm("ipr_resolution_percent")
    for value in (60, 70, 80):
        panel.canvas.set_zoom(value)
        spin(0.04)
    assert panel.entry.session is session and rop.evalParm("ipr_resolution_percent") == percent
    wait((512, 288))
    record("A burst of zoom changes is debounced before the final resolution update")
    panel.cancel()
    spin(0.1)
    session = panel.entry.session
    panel.canvas.set_zoom(150)
    spin(0.5)
    assert panel._stopped and panel.entry.session is session and rop.evalParm("ipr_resolution_percent") == 150
    panel.render()
    wait((960, 540))
    panel.pause(True)
    spin(0.1)
    session = panel.entry.session
    panel.canvas.set_zoom(176)
    spin(0.5)
    assert panel.entry.paused and panel.entry.session is session
    panel.pause(False)
    wait((1126, 633))
    record("Stop and Pause hold the worker; explicit Play/Resume applies the latest linked resolution")
    frozen = panel.canvas.image.copy()
    frozen.setText("cycles:pixel_aspect", "2")
    panel.add_history(frozen, "Different pixel aspect")
    session = panel.entry.session
    percent = rop.evalParm("ipr_resolution_percent")
    panel.snapshot_menu.setCurrentIndex(1)
    spin(0.05)
    assert panel.canvas.linked_reference is None and panel.canvas.pixel_aspect() == 2
    panel.canvas.set_zoom(50)
    spin(0.5)
    assert panel.entry.session is session and rop.evalParm("ipr_resolution_percent") == percent
    panel.snapshot_menu.setCurrentIndex(0)
    zoom(176)
    assert panel.canvas.linked_reference is not None and panel.canvas.pixel_aspect() == 1
    record("Snapshots retain their own pixel aspect and never resize the live worker while selected")
    rect = panel.canvas.image_rect()
    session = panel.entry.session
    generation = session.status()["generation"]
    panel.resolution_link.click()
    spin(0.5)
    assert not panel.keep_image_size.isChecked() and panel.keep_image_size.isEnabled()
    assert panel.preview_resolution.isEnabled() and math.isclose(
        rop.evalParm("ipr_resolution_percent"), 176, abs_tol=1e-7
    ), (panel.preview_resolution.isEnabled(), rop.evalParm("ipr_resolution_percent"))
    assert panel.entry.session is session and session.status()["generation"] == generation
    assert abs(panel.canvas.image_rect().width() - rect.width()) < 1e-6
    record("Unlink retains the last resolution and framing without restarting; the prior size-lock setting is restored")
    panel.resolution_link.click()
    zoom(87.5)
    assert rop.evalParm("resolution_percent") == 75
    assert output.dimensions(rop, hou.time(), interactive=False) == (480, 270)
    rop.render(frame_range=(1, 1))
    image = oiio.ImageInput.open(str(dest.parent / "disk.exr"))
    assert image, oiio.geterror()
    assert (image.spec().width, image.spec().height) == (480, 270)
    image.close()
    record("Disk/SOHO dimensions stay at Common Settings 75%, independently of linked IPR")
    path = rop.path()
    hip = dest.parent / "linked.hiplc"
    panel._viewer_state.save(force=True)
    hou.hipFile.save(str(hip))
    panel.dispose()
    panel.close()
    registry.close_all()
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    rop = hou.node(path)
    panel = RenderPanel()
    panel.resize(1400, 800)
    panel.canvas.setFixedSize(560, 315)
    panel.show()
    panel.render()
    wait((560, 315))
    assert panel.resolution_link.isChecked() and panel._rop == rop
    assert not panel._resolution_linker.manual_keep
    record("HIP reload restores the linked mode, base-relative zoom and previous manual size-lock preference")
    other = RenderPanel()
    other.set_rop(rop)
    other.resolution_link.setChecked(True)
    assert not panel.resolution_link.isChecked() and other.resolution_link.isChecked()
    other.dispose()
    other.close()
    record("Only one viewer controls a shared ROP link, preventing competing Fit resolutions")
    report.update(result="passed", device_pixel_ratio=panel.canvas.devicePixelRatioF())
except BaseException:
    import traceback

    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    panel.dispose()
    panel.close()
    registry.close_all()
    dest.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
