# SPDX-License-Identifier: Apache-2.0
"""Viewer state round-trip through HIP files using current source and staged native module."""

from support.paths import artifacts_root

import json, os, sys, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
from PySide6 import QtWidgets, QtGui
import cycles_houdini

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
cycles_houdini.__path__.insert(0, str(root / "houdini/python3.13libs/cycles_houdini"))
from cycles_houdini.panel import RenderPanel
from cycles_houdini import registry

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
out = artifacts_root() / "viewer-state-test"
out.mkdir(parents=True, exist_ok=True)
first = out / "viewer-a.hip"
second = out / "viewer-b.hip"
rop = hou.node("/out").createNode("cycles_ipr", "saved_render")
other = hou.node("/out").createNode("cycles_ipr", "other_render")
cam = hou.node("/obj").createNode("cam", "saved_camera")
cam.parm("tz").set(5)
geo = hou.node("/obj").createNode("geo", "source", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=box.path(),
        resx=64,
        resy=48,
        ipr_samples=1048576,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
    )
)
from cycles_houdini.device_selection import set_devices

set_devices(rop, "CPU")
rop.parm("picture").set(str(out / "images" / "render.$F4.exr"))
rop.setParms(dict(output_color_override=1, output_color_space="ACES2065-1"))
p = RenderPanel()
p.set_rop(rop)
p.choose_camera(cam)
p.show()
p.render()
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    app.processEvents()
    p.poll()
    if p.entry and p.entry.session.status()["state"] == "rendering" and not p.canvas.image.isNull():
        break
    time.sleep(0.01)
else:
    raise AssertionError("IPR did not start")
# Save a real render in each format while IPR is active. No file chooser is involved.
hou.hipFile.save(str(first))
saved = []
for kind in ("png", "png", "jpg", "exr"):
    if kind == "exr":
        p.cancel()
        rop.parm("ipr_samples").set(8)
        p.render()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            app.processEvents()
            p.poll()
            if p.entry and p.entry.session.status()["state"] == "complete":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("EXR render did not complete")
    p.save_format.setCurrentIndex(p.save_format.findData(kind))
    p.update_save_control()
    assert p.save_button.isEnabled(), (kind, p.info.text())
    p.save_button.click()
    assert p._pending_save is not None, p.info.text()
    filename = Path(p._pending_save["filename"])
    deadline = time.monotonic() + 30
    while p._pending_save and time.monotonic() < deadline:
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    assert p._pending_save is None and p.info.text().startswith("Saved "), p.info.text()
    assert filename.exists() and filename.stat().st_size > 0
    saved.append(filename)
assert len(set(saved)) == 4
# Existing files must never be replaced, even if another writer wins the race.
from cycles_houdini import viewer_save

before = saved[0].read_bytes()
event = viewer_save.begin_image(p.canvas.image.copy(), str(saved[0]), "png")
assert event.wait(10) and isinstance(event.error, FileExistsError)
assert saved[0].read_bytes() == before
import OpenImageIO as oiio

for path in saved:
    reader = oiio.ImageInput.open(str(path))
    assert reader, path
    assert reader.spec().width > 0 and reader.spec().height > 0
    if path.suffix == ".exr":
        assert reader.spec().get_string_attribute("oiio:ColorSpace") == "ACES2065-1"
    reader.close()
assert saved[0].parent == out / "images"
p.save_format.setCurrentIndex(p.save_format.findData("png"))
p.auto.setChecked(False)
p.channels.setCurrentIndex(p.channels.findData("r"))
p.channel_alpha.setChecked(False)
p.background_button.set_color(QtGui.QColor("#43718a"))
p.background_button.setChecked(True)
p.exposure.setValue(1.37)
p.gamma.setValue(1.21)
p.canvas.set_zoom(150)
hou.hipFile.save(str(first))
assert not p._viewer_state.allow_follow()
p.set_rop(other)
p.exposure.setValue(0)
hou.hipFile.load(str(first), suppress_save_prompt=True)
assert p._rop.path() == "/out/saved_render", p._rop
assert p._rop.evalParm("camera") == "/obj/saved_camera"
assert p.camera.currentData() == "/obj/saved_camera"
assert not p.auto.isChecked() and p.channels.currentData() == "r" and not p.channel_alpha.isChecked()
assert p.background_button.isChecked() and not p.checker.isChecked()
assert p.canvas.background_color.name() == "#43718a"
assert abs(p.exposure.value() - 1.37) < 0.001 and abs(p.gamma.value() - 1.21) < 0.001
assert p.canvas.view_mode == "manual" and p.save_format.currentData() == "png"
assert not p._viewer_state.allow_follow()
p.dispose()
p.close()
reopened = RenderPanel()
assert reopened._rop.path() == "/out/saved_render" and reopened.camera.currentData() == "/obj/saved_camera"
assert abs(reopened.exposure.value() - 1.37) < 0.001
# A second idle viewer must not overwrite changed preferences when the file is saved.
second_view = RenderPanel()
reopened.exposure.setValue(2.25)
hou.hipFile.save(str(first))
second_view.dispose()
second_view.close()
hou.hipFile.load(str(first), suppress_save_prompt=True)
assert abs(reopened.exposure.value() - 2.25) < 0.001
reopened.exposure.setValue(1.37)
hou.hipFile.save(str(first))
hou.hipFile.clear(suppress_save_prompt=True)
assert reopened._rop is None and reopened.exposure.value() == 0 and reopened.checker.isChecked()
fresh = hou.node("/out").createNode("cycles_ipr", "new_render")
reopened.set_rop(fresh)
hou.hipFile.save(str(second))
hou.hipFile.load(str(first), suppress_save_prompt=True)
assert reopened._rop.path() == "/out/saved_render" and abs(reopened.exposure.value() - 1.37) < 0.001
hou.hipFile.load(str(second), suppress_save_prompt=True)
assert reopened._rop.path() == "/out/new_render" and reopened.exposure.value() == 0
reopened.dispose()
reopened.close()
registry.close_all()
(out / "report.json").write_text(
    json.dumps(
        dict(
            result="passed",
            checks=[
                "HIP save/load",
                "camera on ROP",
                "viewer recreation",
                "scene clear and switching",
                "display settings",
                "save with IPR running",
                "collision-safe EXR PNG JPEG output",
                "EXR ROP color-space override",
            ],
        ),
        indent=2,
    )
)
print("VIEWER STATE TEST PASSED")
