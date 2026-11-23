# SPDX-License-Identifier: Apache-2.0
"""Viewer percentages never edit disk resolution in either sampling mode."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
import OpenImageIO as oiio
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import native_scene, output, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
dest = artifacts_root() / "stage3/viewer-resolution"
dest.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "resolution_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
cam = hou.node("/obj").createNode("cam", "resolution_camera")
cam.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "independent_resolution")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=cam.path(),
        resx=160,
        resy=100,
        resolution_percent=75,
        ipr_resolution_percent=50,
        samples=8,
        ipr_samples=4,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        render_threads=2,
        picture=str(dest / "disk.exr"),
        output_compression="zip",
        output_precision="float",
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        allowmotionblur=0,
    )
)
p = RenderPanel()
p.resize(1280, 700)
p.set_rop(rop)
p.show()
p.render()
checks = []


def wait_image(size):
    end = time.monotonic() + 30
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        if p.entry:
            status = p.entry.session.status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"])
            if (
                status["state"] == "complete"
                and p.canvas.full_size == size
                and not p.canvas.image.isNull()
                and not p._pending
                and not p._render_queued
                and not p._in_render
            ):
                return
        time.sleep(0.005)
    raise TimeoutError(p.info.text())


def percentage(text):
    editor = p.preview_resolution.lineEdit()
    editor.setFocus()
    editor.selectAll()
    QtTest.QTest.keyClicks(editor, text)
    QtTest.QTest.keyClick(editor, QtCore.Qt.Key_Return)


try:
    wait_image((80, 50))
    percentage("25%")
    wait_image((40, 25))
    assert rop.evalParm("resolution_percent") == 75 and rop.evalParm("ipr_resolution_percent") == 25
    p.mode_menu.setCurrentIndex(1)
    wait_image((40, 25))
    assert not rop.evalParm("viewer_interactive")
    percentage("60%")
    wait_image((96, 60))
    assert rop.evalParm("resolution_percent") == 75 and rop.evalParm("ipr_resolution_percent") == 60
    assert native_scene.configuration(rop)[1:3] == (96, 60)
    assert native_scene.configuration(rop, interactive=False)[1:3] == (120, 75)
    checks.append("Actual viewer percentage edits affect only IPR Resolution % in both sampling modes")

    rop.parm("resolution_percent").set(80)
    p.sync_preview_controls()
    assert p.preview_resolution.currentText() == "60%"
    assert output.dimensions(rop, hou.time()) == (96, 60)
    assert output.dimensions(rop, hou.time(), interactive=False) == (128, 80)
    percentage("ROP %")
    wait_image((128, 80))
    assert rop.evalParm("ipr_resolution_percent") == 0 and rop.evalParm("resolution_percent") == 80
    percentage("25%")
    wait_image((40, 25))
    serial = p._update_serial
    percentage("25%")
    assert p._update_serial == serial, "Unchanged percentage restarted rendering"
    percentage("500%")
    assert p.preview_resolution.currentText() == "25%" and rop.evalParm("resolution_percent") == 80
    assert rop.evalParm("ipr_resolution_percent") == 25
    checks.append(
        "ROP % is explicit follow mode; independent percentages ignore final scaling; invalid/unchanged edits are safe"
    )

    # The real disk/SOHO path must ignore viewer resolution even while the
    # viewer uses Final sampling settings and its own worker stays open.
    rop.render(frame_range=(1, 1))
    image = oiio.ImageInput.open(str(dest / "disk.exr"))
    assert image, oiio.geterror()
    assert (image.spec().width, image.spec().height) == (128, 80)
    image.close()
    rop.setParms(dict(render_region=1, crop_to_region=1, picture=str(dest / "cropped.exr")))
    rop.parmTuple("render_region_bounds").set((0.25, 0.75, 0.25, 0.75))
    rop.render(frame_range=(1, 1))
    image = oiio.ImageInput.open(str(dest / "cropped.exr"))
    assert image, oiio.geterror()
    assert (image.spec().width, image.spec().height) == (64, 40)
    image.close()
    assert rop.evalParm("resolution_percent") == 80 and rop.evalParm("ipr_resolution_percent") == 25
    checks.append(
        "Disk render and cropped disk render use final dimensions while the live Final-mode viewer has a different percentage"
    )
finally:
    p.dispose()
    p.close()
    registry.close_all()
    app.processEvents()
report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-viewer-resolution-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
