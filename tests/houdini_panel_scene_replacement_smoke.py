# SPDX-License-Identifier: Apache-2.0
"""The viewer survives its ROP disappearing when a scene is cleared or opened.

Reproduces the artist report: opening a scene while the Resolution field had
focus ran its editingFinished handler against the deleted ROP and raised
hou.ObjectWasDeleted twice. Also checks that leaving the field with an
unchanged value never restarts rendering, that switching Interactive/Final
always starts a fresh render (even when both modes resolve to identical
settings), and that the panel renders again after binding a ROP in the new scene.
"""

from support.paths import artifacts_root

import os, json, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
from PySide6 import QtWidgets
from cycles_houdini import device_selection
from cycles_houdini.panel import RenderPanel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage1/panel-scene-replacement"
dest.mkdir(parents=True, exist_ok=True)
checks = []


def scene():
    geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
    box = geo.createNode("box")
    box.setRenderFlag(True)
    cam = hou.node("/obj").createNode("cam")
    cam.parm("tz").set(5)
    rop = hou.node("/out").createNode("cycles_ipr")
    device_selection.set_devices(rop, "CPU")
    rop.setParms(
        dict(
            source_mode="sop",
            soppath=box.path(),
            camera=cam.path(),
            resx=160,
            resy=100,
            samples=4,
            ipr_samples=4,
            ipr_use_denoise=0,
            use_denoise=0,
            allowmotionblur=0,
        )
    )
    for name in ("resolution_percent", "ipr_resolution_percent"):
        assert rop.parm(name) is not None, name
        rop.parm(name).set(100)
    return rop, cam


def wait(p, test):
    end = time.monotonic() + 30
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        if test():
            return
        time.sleep(0.005)
    raise TimeoutError(p.info.text())


rop, cam = scene()
p = RenderPanel()
p.resize(1280, 800)
p.set_rop(rop)
p.show()
p.render()
try:
    wait(p, lambda: p.entry and p.entry.session.status()["state"] == "complete")
    generation = p.entry.session.status()["generation"]
    serial = p._update_serial
    edit = p.preview_resolution.lineEdit()
    edit.setText(p.preview_resolution.currentText())
    edit.editingFinished.emit()
    for _ in range(20):
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    assert p._update_serial == serial and p.entry.session.status()["generation"] == generation, (
        "Unchanged resolution restarted rendering"
    )
    checks.append("Leaving the Resolution field unchanged does not restart rendering")

    # Both modes resolve to identical settings here, so only an explicit
    # restart produces a new render generation.
    for interactive, label in ((False, "Final"), (True, "Interactive")):
        before = p.entry.session.status()["generation"]
        p.preview_interactive.setChecked(interactive)
        assert bool(rop.evalParm("viewer_interactive")) == interactive
        wait(
            p,
            lambda: p.entry.session.status()["generation"] > before and p.entry.session.status()["state"] == "complete",
        )
        if hasattr(p, "mode_menu"):
            assert p.mode_menu.currentText() == label, p.mode_menu.currentText()
    settled = lambda: (
        not p._pending
        and not p._render_queued
        and p.entry.session.status()["state"] == "complete"
        and not p.entry.session.status().get("pending_update")
    )
    wait(p, settled)
    for _ in range(20):
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    wait(p, settled)
    generation = p.entry.session.status()["generation"]
    p.change_preview_interactive(True)
    for _ in range(20):
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    assert p.entry.session.status()["generation"] == generation, "Re-selecting the current mode restarted rendering"
    checks.append("Switching Interactive/Final starts a fresh render each time; re-selecting the current mode does not")

    hou.hipFile.clear(suppress_save_prompt=True)
    assert p._rop is None, "Panel kept a deleted ROP handle"
    # Every ROP-bound widget callback now runs against "no ROP" without raising.
    edit.setText("50%")
    edit.editingFinished.emit()
    p.change_preview_resolution()
    p.sync_preview_controls()
    p.change_preview_denoise(True)
    p.change_preview_interactive(False)
    p.change_preview_interactive(True)
    p.lock_camera(True)
    p.lock_camera(False)
    p.update_render_controls()
    for _ in range(20):
        app.processEvents()
        p.poll()
        time.sleep(0.01)
    assert not p.render_button.isEnabled(), "Render stayed enabled without a ROP"
    checks.append(
        "After clearing the scene, resolution, denoise, mode, camera lock and polling see no ROP and raise nothing"
    )

    rop, cam = scene()
    p.set_rop(rop)
    p.render()
    wait(p, lambda: p.entry and p.entry.session.status()["state"] == "complete" and not p.canvas.image.isNull())
    checks.append("Binding a ROP in the new scene renders again")
finally:
    p.dispose()
    p.close()
    app.processEvents()
report = dict(result="passed", scope=__doc__, checks=checks)
(dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
