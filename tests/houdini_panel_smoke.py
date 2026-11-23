# SPDX-License-Identifier: Apache-2.0
"""Exercise the actual Qt widget and host callbacks in a headless Houdini process."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import time
import hou
from PySide6 import QtWidgets, QtCore, QtGui
from cycles_houdini import registry, passes
from cycles_houdini.panel import RenderPanel
from panel_navigation_checks import check_navigation


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1"
destination.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "panel_geometry", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "panel_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "panel_test")
set_rop_devices(rop, "CPU")
passes.add(rop, "normal")
for name, value in dict(soppath=box.path(), camera=camera.path(), resx=320, resy=180, samples=16).items():
    rop.parm(name).set(value)
panel = RenderPanel()
panel.resize(900, 650)
panel.set_rop(rop)
panel.show()
# An artist can select the ROP in a second panel before another viewer starts it.
waiting_panel = RenderPanel()
waiting_panel.set_rop(rop)
assert waiting_panel.entry is None
panel.render()
assert panel.entry is not None, panel.info.text()


def wait(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        if predicate():
            return
        time.sleep(0.005)
    raise TimeoutError(panel.info.text())


wait(lambda: panel.entry.session.status()["state"] == "complete" and not panel.canvas.image.isNull())
initial = panel.entry.session.status()
image = panel.canvas.image.copy()
assert initial["progress"] == 1.0, initial
entry_before_reselection = panel.entry
panel.set_rop(rop)
assert panel.entry is entry_before_reselection, "Selecting the same ROP must retain the artist render session"
assert panel.entry.session.status()["generation"] == initial["generation"]
assert panel.canvas.image == image
waiting_panel.poll()
assert waiting_panel.entry is entry_before_reselection, (
    "A preselected panel must join a render started later by another viewer"
)
assert entry_before_reselection.views == 2 and len(registry.entries()) == 1
waiting_panel.set_rop(rop)
assert entry_before_reselection.views == 2
assert entry_before_reselection.session.status()["generation"] == initial["generation"]
waiting_panel.dispose()
waiting_panel.close()
assert entry_before_reselection.views == 1
waiting_panel.set_rop(rop)
waiting_panel.render()
assert waiting_panel.entry is None and waiting_panel._observer is None
assert entry_before_reselection.views == 1
assert entry_before_reselection.session.status()["generation"] == initial["generation"]
panel.memory_details.setChecked(True)
panel.poll_memory(force=True)
panel.poll()
assert panel.memory_info.text().startswith("Mem: "), panel.memory_info.text()
assert "CPU rendering" in panel.memory_info.toolTip(), panel.memory_info.toolTip()
assert panel.memory_info.parentWidget() is panel.viewer_status
assert panel.memory_info.mapTo(panel, QtCore.QPoint()).y() == panel.info.mapTo(panel, QtCore.QPoint()).y()
assert "NVIDIA" not in panel.memory_info.text() and "AMD" not in panel.memory_info.text()
assert panel.entry.session.status()["generation"] == initial["generation"]
memory_text = panel.memory_info.text()
# A display background must never recapture or alter the rendered image/alpha.
for control in (panel.exposure, panel.gamma):
    control.setValue(1.374)
    assert control.value() == 1.37
    control.setValue(1.376)
    assert control.value() == 1.38
    assert control.input.findChildren(QtWidgets.QLineEdit)[0].text() == "1.38"
    control.setValue(control.default)
background = panel.background_button
assert background.parentWidget() is panel.checker.parentWidget()
assert background.swatch.size() == panel.checker.size()
for control in (panel.checker, background, background.swatch, panel.actual_pixels, panel.fit_button):
    assert control.size() == panel.checker.size() and control.width() == control.height(), control.size()
    assert control.parentWidget().height() == panel.channel_buttons["rgba"].parentWidget().height()
assert background.swatch.parentWidget() is background.parentWidget()
assert background.width() + background.swatch.width() > panel.checker.width()
assert panel.viewer_status.progress.x() - panel.viewer_status.metrics.geometry().right() <= 12
assert panel.memory_info.geometry().right() > panel.width() * 0.75
assert panel.viewer_status.metrics.x() < panel.width() / 2
original_background = QtGui.QColor(panel.canvas.background_color)
original_checker = panel.checker.isChecked()
original_background_enabled = panel.canvas.background_enabled
background.swatch.click()
app.processEvents()
assert background.dialog and not panel.checker.isChecked()
background.dialog.setCurrentColor(QtGui.QColor("#286cb0"))
app.processEvents()
assert panel.canvas.background_color == QtGui.QColor("#286cb0")
background.dialog.reject()
app.processEvents()
assert panel.canvas.background_color == original_background
assert panel.checker.isChecked() == original_checker
assert panel.canvas.background_enabled == original_background_enabled
background.swatch.click()
app.processEvents()
background.dialog.setCurrentColor(QtGui.QColor("#286cb0"))
background.dialog.accept()
app.processEvents()
assert background.dialog is None and background.isChecked() and not panel.checker.isChecked()
assert panel.canvas.background_color == QtGui.QColor("#286cb0")
background.click()
app.processEvents()
assert not background.isChecked() and not panel.checker.isChecked() and not panel.canvas.background_enabled
assert background.dialog is None and panel.canvas.background_color == QtGui.QColor("#286cb0")
background.click()
app.processEvents()
assert background.isChecked() and panel.canvas.background_enabled and background.dialog is None
panel.checker.click()
app.processEvents()
assert panel.checker.isChecked() and not background.isChecked() and not panel.canvas.background_enabled
panel.checker.click()
app.processEvents()
panel.poll()
assert not panel.checker.isChecked() and not background.isChecked() and not panel.canvas.background_enabled
# A background or checker change reconverts the live frame (its scene-linear composites), so
# the canvas may now show a later publication of the same generation: compare the straight
# image with a fresh conversion of the current frame instead of the copy taken earlier.
assert panel.entry.session.status()["generation"] == initial["generation"]
straight_settings = {k: v for k, v in panel.display_options().items() if k != "backgrounds"}
wait(
    lambda: not panel._settings_dirty
    and panel._sequence == panel.entry.session.display_image(**straight_settings)["sequence"]
)
from cycles_houdini.panel import ImageCanvas

current = ImageCanvas.merge_frame(None, panel.entry.session.display_image(**straight_settings))
assert panel.canvas.image.convertToFormat(current.format()) == current

probe = ImageCanvas()
probe.resize(96, 96)
probe.checker_enabled = False
probe.background_color = QtGui.QColor("#286cb0")
probe.background_enabled = True
probe.image = QtGui.QImage(32, 32, QtGui.QImage.Format_ARGB32)
probe.image.fill(QtCore.Qt.transparent)
probe.full_size = (32, 32)
probe.compare_image = probe.image.copy()
probe.show()
app.processEvents()
bounds = probe.image_rect()
painted = probe.grab().toImage()
for fraction in (0.25, 0.75):
    pixel = painted.pixelColor(
        int(bounds.left() + bounds.width() * fraction), int(bounds.top() + bounds.height() * 0.25)
    )
    assert pixel == probe.background_color, (pixel.name(), probe.background_color.name())
assert probe.image.pixelColor(0, 0).alpha() == 0
probe.background_enabled = False
probe.update()
app.processEvents()
from cycles_houdini.viewer_theme import colors

painted = probe.grab().toImage()
for fraction in (0.25, 0.75):
    pixel = painted.pixelColor(
        int(bounds.left() + bounds.width() * fraction), int(bounds.top() + bounds.height() * 0.25)
    )
    assert pixel.rgba() == colors(probe)["canvas"].rgba(), (
        pixel.name(),
        colors(probe)["canvas"].name(),
        pixel.rgba(),
        colors(probe)["canvas"].rgba(),
    )
assert probe.background_color == QtGui.QColor("#286cb0")
probe.close()
navigation = check_navigation(panel)
panel.exposure.setValue(1)
panel.poll()
assert panel.entry.session.status()["generation"] == initial["generation"]
wait(lambda: not panel._settings_dirty and panel.canvas.image != image)
assert panel.canvas.image != image
exposed = panel.canvas.image.copy()
panel.gamma.setValue(2)
panel.poll()
wait(lambda: not panel._settings_dirty and panel.canvas.image != exposed)
assert panel.canvas.image != exposed
assert panel.entry.session.status()["generation"] == initial["generation"]
assert panel.stop_button.accessibleName() == "Stop rendering" and not panel.stop_button.text()
assert not panel.render_button.isEnabled() and panel.pause_button.isEnabled() and panel.stop_button.isEnabled()
from panel_navigation_checks import check_layout

original_size = panel.size()
for width in (520, 900, 1280, 520):
    panel.resize(width, 650)
    app.processEvents()
    check_layout(panel)
    assert panel.width() == max(width, panel.minimumWidth()), (width, panel.minimumSizeHint())
    assert panel.entry.session.status()["generation"] == initial["generation"]
    panel.grab().save(str(destination / f"cycles-panel-width-{width}.png"))
panel.resize(original_size)
app.processEvents()
icon = panel.checker.icon().pixmap(16, 16).toImage()
for y in range(icon.height()):
    for x in range(icon.width()):
        c = icon.pixelColor(x, y)
        assert c.red() == c.green() == c.blue()
normal = panel.entry.session.display_image(**{"pass": "normal"})
assert normal["rgba"] == panel.entry.session.display_image(**{"pass": "normal"}, gamma=2)["rgba"]
alpha = panel.entry.session.display_image(channels="alpha")
assert alpha["rgba"] == panel.entry.session.display_image(channels="alpha", gamma=2)["rgba"]
panel.snapshot()
assert len(panel._snapshots) == 1
panel.set_region((80, 45, 160, 90))
wait(lambda: panel.entry.session.status()["state"] == "complete")
frame = panel.entry.session.frame_info()
assert (frame["x"], frame["y"], frame["width"], frame["height"]) == (80, 45, 160, 90)
assert panel.canvas.full_size == (320, 180)
assert panel.region.isChecked()
panel.region.setChecked(False)
wait(lambda: panel.entry.session.status()["state"] == "complete")
full = panel.entry.session.frame_info()
assert tuple(full[name] for name in ("x", "y", "width", "height")) == (0, 0, 320, 180)
assert panel.entry.crop_override is None and not panel.canvas.region_enabled
generation = frame["generation"]
conversions = panel.entry.session.status()["geometry_conversions"]
camera.parm("tx").set(0.1)
wait(
    lambda: panel.entry.session.status()["generation"] > generation
    and panel.entry.session.status()["state"] == "complete"
)
assert panel.entry.session.status()["geometry_conversions"] == conversions
generation = panel.entry.session.status()["generation"]
box.parm("sizex").set(1.3)
wait(
    lambda: panel.entry.session.status()["generation"] > generation
    and panel.entry.session.status()["state"] == "complete"
)
assert panel.entry.session.status()["geometry_conversions"] == conversions + 1
panel.pause_button.setChecked(True)
panel.pause(True)
wait(lambda: panel.entry.session.status()["state"] == "paused")
panel.cancel()
wait(lambda: panel.entry.session.status()["state"] == "cancelled")
assert not panel.entry.paused and not panel.pause_button.isChecked()
generation = panel.entry.session.status()["generation"]
panel.queue_render()
wait(
    lambda: panel.entry.session.status()["generation"] > generation
    and panel.entry.session.status()["state"] == "complete"
)
index = panel.pass_menu.findData("normal")
assert index >= 0
panel.pass_menu.setCurrentIndex(index)
panel.poll()
wait(lambda: not panel._settings_dirty)
status = panel.entry.session.status()
panel.grab().save(str(destination / "cycles-panel.png"))
entry = panel.entry
panel.dispose()
panel.close()
assert entry.session.status()["state"] == "closed" and entry.session.status()["device_allocation_bytes"] == 0
assert registry.get(entry.key) is None
report = dict(
    result="passed",
    background_color="Separate BG toggle and swatch; checker/color/default transitions retain the chosen color; picker accept/cancel and snapshot alpha without render changes",
    status_alignment="Status/time, samples then progress on the left; memory on the right",
    shared_registry="one session",
    display_edit="no render restart",
    crop=frame,
    status=status,
    navigation=navigation,
    paused_stop_restart="Render resumes tracing after Stop from paused state",
    memory_text=memory_text,
    memory_query="opt-in whole-process telemetry without render restart",
    rop_reselection="same node preserves the session, generation and pixels",
    late_attachment="preselected panel joins a later render without starting or duplicating it",
    disposed_panel="late selection/render callbacks do not revive a closed viewer",
)
(destination / "houdini-panel-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "houdini-panel-smoke.json")
