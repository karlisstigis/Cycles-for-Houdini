# SPDX-License-Identifier: Apache-2.0
"""Compact viewer: native rendering, display-only channels, comparison and complete toolbar visibility."""

from support.paths import artifacts_root
import os, json, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou

# hython only: below, this test replaces the application font, which in the
# Houdini GUI is Houdini's own interface font for the rest of the session.
if hou.isUIAvailable():
    raise SystemExit(
        "houdini_viewer_redesign_smoke.py runs only under hython: it changes the application font, "
        "which in the Houdini GUI would change Houdini's interface font."
    )
import numpy as np
from PySide6 import QtWidgets, QtGui, QtCore, QtTest
from cycles_houdini import registry, device_selection
from cycles_houdini.panel import RenderPanel
from panel_navigation_checks import check_layout

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
# Offscreen Qt does not discover Windows system fonts automatically.
font_file = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/segoeui.ttf"
if font_file.exists():
    QtGui.QFontDatabase.addApplicationFont(str(font_file))
font = QtGui.QFont("Segoe UI")
font.setPixelSize(12)
app.setFont(font)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "viewer-redesign"
dest.mkdir(exist_ok=True)
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
        allowmotionblur=0,
    )
)
# Passes are the multiparm list only; a new ROP has none (the legacy Normal toggle is gone). Author one explicitly.
from cycles_houdini import passes as _passes

_passes.add(rop, "normal")
p = RenderPanel()
p.resize(1440, 900)
p.set_rop(rop)
p.show()
p.render()


def wait(test):
    end = time.monotonic() + 25
    while time.monotonic() < end:
        app.processEvents()
        p.poll()
        if test():
            return
        time.sleep(0.005)
    raise TimeoutError(p.info.text())


try:
    wait(lambda: p.entry and p.entry.session.status()["state"] == "complete" and not p.canvas.image.isNull())
    from cycles_houdini.panel import StatusLine

    diagnostic = StatusLine()
    events = []
    diagnostic.textChanged.connect(events.append)
    for _ in range(100):
        diagnostic.setText("Complete", tooltip="Complete\nSource warning")
    assert events == ["Complete"] and diagnostic.toolTip() == "Complete\nSource warning"
    diagnostic.setText("Complete", tooltip="Complete\nRepaired")
    assert diagnostic.toolTip() == "Complete\nRepaired"
    diagnostic.setText("Rendering")
    assert events[-1] == "Rendering"
    diagnostic.deleteLater()
    for menu, node in ((p.rop_path, rop), (p.camera, cam)):
        assert menu.currentText() == node.name()
        assert menu.toolTip() == node.path()
        assert menu.itemData(menu.currentIndex(), QtCore.Qt.ToolTipRole) == node.path()
    # Inventory is refreshed on opening, including subnet cameras and live repairs.
    other = hou.node("/obj").createNode("subnet", "menu_camera_container")
    extra = other.createNode("cam", "camera_added_after_viewer_opened")
    extra.parm("tz").set(7)
    p.camera.refresh()
    assert p.camera.findData(extra.path()) >= 0
    assert p.camera.itemText(p.camera.findData(extra.path())) == extra.name()
    duplicate = other.createNode("cam", cam.name())
    p.camera.refresh()
    same_names = [i for i in range(p.camera.count()) if p.camera.itemText(i) == cam.name()]
    assert len(same_names) == 2
    assert {p.camera.itemData(i, QtCore.Qt.ToolTipRole) for i in same_names} == {cam.path(), duplicate.path()}
    index = p.camera.findData(duplicate.path())
    p.camera.setCurrentIndex(index)
    p.camera.activated.emit(index)
    assert rop.evalParm("camera") == duplicate.path() and p.camera.toolTip() == duplicate.path()
    index = p.camera.findData(extra.path())
    p.camera.setCurrentIndex(index)
    p.camera.activated.emit(index)
    assert rop.evalParm("camera") == extra.path()
    extra.setName("renamed_camera")
    p.camera.refresh()
    assert p.camera.currentData() == extra.path()
    assert p.camera.currentText() == extra.name() and p.camera.toolTip() == extra.path()
    p.camera.set_path(cam.path())
    p.camera.activated.emit(p.camera.currentIndex())
    assert rop.evalParm("camera") == cam.path()
    extra.destroy()
    p.camera.refresh()
    assert all("renamed_camera" not in p.camera.itemText(i) for i in range(p.camera.count()))
    recreated = other.createNode("cam", "repaired_camera")
    p.camera.refresh()
    assert p.camera.findData(recreated.path()) >= 0
    second = rop.parent().createNode("cycles_ipr", "menu_second_render")
    p.rop_path.refresh()
    assert p.rop_path.findData(second.path()) >= 0
    index = p.rop_path.findData(second.path())
    p.rop_path.setCurrentIndex(index)
    p.rop_path.activated.emit(index)
    assert p._rop == second
    p.rop_path.set_path(rop.path())
    p.rop_path.activated.emit(p.rop_path.currentIndex())
    assert p._rop == rop
    p.render()
    wait(lambda: p.entry and p.entry.session.status()["state"] == "complete" and not p._settings_dirty)
    # Folder targets use the evaluated ROP output path, then a successful
    # explicit Save EXR location. Mock only Explorer launch, retaining the click.
    from unittest.mock import patch

    original_picture = rop.parm("picture").unexpandedString()
    rop.parm("picture").set(str(dest / "frame.$F4.exr"))
    opened = []
    with patch.object(
        QtGui.QDesktopServices, "openUrl", side_effect=lambda url: opened.append(url.toLocalFile()) or True
    ):
        p.output_folder_button.click()
    assert Path(opened[-1]) == dest
    # Save creates a unique file beside the evaluated ROP picture without a chooser.
    from types import SimpleNamespace

    with patch.object(
        hou,
        "ui",
        SimpleNamespace(selectFile=lambda **_: (_ for _ in ()).throw(AssertionError("Unexpected save dialog"))),
        create=True,
    ):
        p.save_button.click()
    assert p._pending_save is not None and not p.save_button.isEnabled()
    saved = Path(p._pending_save["filename"])
    assert "Saving EXR" in p.info.text()
    wait(lambda: p._pending_save is None)
    assert "Saved EXR" in p.info.text(), p.info.text()
    assert saved.is_file() and p.image_output_directory() == dest
    assert p.save_button.isEnabled()
    with patch.object(
        QtGui.QDesktopServices, "openUrl", side_effect=lambda url: opened.append(url.toLocalFile()) or True
    ):
        p.output_folder_button.click()
    assert Path(opened[-1]) == dest
    del p._saved_output_directories[rop.sessionId()]
    rop.parm("picture").set(str(dest / "not-created" / "image.exr"))
    with patch.object(QtGui.QDesktopServices, "openUrl") as launch:
        p.output_folder_button.click()
        assert not launch.called
    assert "does not exist" in p.info.text()
    rop.parm("picture").set(original_picture)
    generation = p.entry.session.status()["generation"]
    conversions = p.entry.session.status()["geometry_conversions"]
    assert not p.render_button.isEnabled() and p.stop_button.isEnabled()
    p.channels.setCurrentIndex(p.channels.findData("rgb"))
    wait(lambda: not p._settings_dirty)
    rgb = p.canvas.image.copy()
    for token, component in (("r", 0), ("g", 1), ("b", 2)):
        p.channel_buttons[token].click()
        wait(lambda: not p._settings_dirty)
        for x, y in ((80, 50), (40, 25), (0, 0)):
            before = rgb.pixelColor(x, y)
            after = p.canvas.image.pixelColor(x, y)
            value = (before.red(), before.green(), before.blue())[component]
            assert after.red() == after.green() == after.blue() == value
    normal = p.pass_menu.findData("normal")
    assert normal >= 0
    p.pass_menu.setCurrentIndex(normal)
    wait(lambda: not p._settings_dirty)
    assert p.display_options()["channels"] == "rgba" and not p.channel_buttons["b"].isEnabled()
    p.pass_menu.setCurrentIndex(p.pass_menu.findData("combined"))
    wait(lambda: not p._settings_dirty)
    p.channels.setCurrentIndex(p.channels.findData("rgba"))
    wait(lambda: not p._settings_dirty)
    p.snapshot()
    snapshot = p._snapshots[0]
    # Custom history rail is integer-valued and selects actual snapshots.
    end = QtCore.QPoint(p.history_slider.width() - 8, p.history_slider.height() // 2)
    QtTest.QTest.mouseClick(p.history_slider, QtCore.Qt.LeftButton, pos=end)
    assert p.snapshot_menu.currentIndex() == 0
    QtTest.QTest.mouseClick(p.history_slider, QtCore.Qt.LeftButton, pos=QtCore.QPoint(8, end.y()))
    assert p.snapshot_menu.currentIndex() == 1
    p.snapshot_compare.click()
    app.processEvents()
    assert p.displaying_live() and p.canvas.compare_image == snapshot and p.canvas.image == p._live_image
    target = p.canvas.image_rect()
    point = QtCore.QPoint(int(target.center().x()), int(target.center().y()))
    QtTest.QTest.mousePress(p.canvas, QtCore.Qt.LeftButton, pos=point)
    QtTest.QTest.mouseMove(p.canvas, point + QtCore.QPoint(30, 0))
    QtTest.QTest.mouseRelease(p.canvas, QtCore.Qt.LeftButton, pos=point + QtCore.QPoint(30, 0))
    assert p.canvas.compare_fraction > 0.5
    p.snapshot_compare.click()
    assert p.canvas.compare_image.isNull() and p.canvas.image == snapshot
    p.snapshot_menu.setCurrentIndex(0)
    p.exposure.setValue(1)
    p.gamma.setValue(1.3)
    p.reset_display.click()
    assert p.exposure.value() == 0 and p.gamma.value() == 1
    for field, label, default in ((p.exposure, p.exposure_label, 0), (p.gamma, p.gamma_label, 1)):
        for target in (field.input, label):
            field.setValue(2)
            QtTest.QTest.mouseClick(target, QtCore.Qt.MiddleButton, QtCore.Qt.ControlModifier)
            assert field.value() == default
    p.display.addItem("A deliberately long OCIO display entry that must remain fully readable")
    # Closed compact fields expose the full selected value as well as usage
    # help; the popup and the tooltip both remain readable without widening UI.
    selected = p.display.currentIndex()
    p.display.blockSignals(True)
    p.display.setCurrentIndex(p.display.count() - 1)
    p.display.blockSignals(False)
    with patch.object(QtWidgets.QToolTip, "showText") as tooltip:
        app.sendEvent(
            p.display,
            QtGui.QHelpEvent(QtCore.QEvent.ToolTip, QtCore.QPoint(3, 3), p.display.mapToGlobal(QtCore.QPoint(3, 3))),
        )
        assert p.display.currentText() in tooltip.call_args.args[1]
        assert "OCIO" in tooltip.call_args.args[1]
    p.display.blockSignals(True)
    p.display.setCurrentIndex(selected)
    p.display.blockSignals(False)
    p.display.showPopup()
    app.processEvents()
    assert (
        p.display.view().minimumWidth()
        >= p.display.fontMetrics().horizontalAdvance(p.display.itemText(p.display.count() - 1)) + 30
    )
    p.display.hidePopup()
    p.display.removeItem(p.display.count() - 1)
    wait(lambda: not p._settings_dirty)
    assert p.entry.session.status()["generation"] == generation
    assert p.entry.session.status()["geometry_conversions"] == conversions
    # Independent solid images catch a missing/reversed comparison layer.
    original = p._live_image
    live = QtGui.QImage(32, 32, QtGui.QImage.Format_RGBA8888)
    live.fill(QtGui.QColor("blue"))
    reference_image = QtGui.QImage(32, 32, QtGui.QImage.Format_RGBA8888)
    reference_image.fill(QtGui.QColor("green"))
    p._live_image = live
    p.add_history(reference_image, "Independent comparison reference")
    p.snapshot_menu.setCurrentIndex(2)
    p.canvas.compare_fraction = 0.5
    p.snapshot_compare.click()
    app.processEvents()
    target = p.canvas.image_rect()
    # Read back what the canvas draws. Without an OpenGL context (the offscreen
    # platform) the GPU canvas must not be chosen: it would stay black.
    from cycles_houdini.viewer import viewer_display

    if viewer_display.mode()[0] == "gpu" and p.canvas.display_mode != "gpu":
        assert viewer_display.opengl_problem() and "OpenGL unavailable" in p.canvas.display_note, (
            viewer_display.describe(p.canvas)
        )
    pixels = p.canvas.grab().toImage()
    for fraction, color in ((0.25, "green"), (0.75, "blue")):
        assert pixels.pixelColor(
            int(target.left() + target.width() * fraction), int(target.center().y())
        ) == QtGui.QColor(color)
    p.snapshot_compare.click()
    p._live_image = original
    p.snapshot_menu.setCurrentIndex(0)
    captures = []
    # The artist reference supplies the picture only for layout screenshots.
    reference = next((root / "UI_mockup/Render viewer — redesign-html/assets").glob("*.jpg"), None)
    picture = QtGui.QImage(str(reference)) if reference else p.canvas.image.copy()
    p.canvas.image = picture
    p.canvas.full_size = picture.width(), picture.height()
    p.canvas.fit_image()
    for width in (1440, 1280, 900, 520, 1280):
        p.resize(width, 900)
        app.processEvents()
        for row in p.toolbar_rows:
            row.arrange()
        app.processEvents()
        check_layout(p)
        assert p.width() == max(width, p.minimumWidth()), (width, p.minimumSizeHint())
        heights = [row.height() for row in p.toolbar_rows]
        if width >= 1280:
            # Reference CSS: 24px controls, 14px icons, 3px inner gaps,
            # fixed-width source fields. Extra panel width must not spread them.
            for control in (p.render_button, p.auto, p.region, p.preview_denoise, p.checker, p.fit_button):
                assert control.iconSize() == QtCore.QSize(14, 14), (control.accessibleName(), control.iconSize())
            # Region, Denoise and Clay are icon-only 20 px squares with a name and a tooltip naming them.
            for control, name in ((p.region, "Region"), (p.preview_denoise, "Denoise"), (p.clay, "Clay")):
                assert not control.text() and not control.icon().isNull(), name
                assert control.toolButtonStyle() == QtCore.Qt.ToolButtonIconOnly, name
                assert control.isCheckable() and control.accessibleName(), name
                assert control.toolTip().startswith(name + ": "), control.toolTip()
                assert control.size() == QtCore.QSize(20, 20), (name, control.size())
            assert p.clay.property("cycles_glyph") == "SHOP_material"
            # Joined buttons are segmented: first rounds its left, last its right corners.
            for frame in p.findChildren(QtWidgets.QWidget):
                if frame.property("cycles_cluster") == "transport" and frame.isVisibleTo(p):
                    parts = sorted(
                        (
                            w
                            for w in frame.findChildren(QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly)
                            if w.isVisibleTo(p)
                        ),
                        key=lambda w: w.x(),
                    )
                    first, last = parts[0], parts[-1]
                    assert first.property("cycles_transport_start") or first.property("cycles_primary"), (
                        first.accessibleName()
                    )
                    assert last.property("cycles_transport_end"), last.accessibleName()
            # Top-row controls share the second row's 20 px control height (compact clusters).
            assert p.render_button.height() == 20 and p.pause_button.height() == 20, (
                p.render_button.height(),
                p.pause_button.height(),
                p.rop_path.width(),
                p.camera.width(),
                p.exposure.input.width(),
            )
            assert p.rop_path.width() == 144 and p.camera.width() == 128
            assert p.exposure.input.width() == 38
            for token in ("rgba", "rgb"):
                control = p.channel_buttons[token]
                assert control.width() >= control.fontMetrics().horizontalAdvance(control.text()) + 8
            for combo in (p.preview_resolution, p.zoom_menu, p.mode_menu):
                assert combo.width() - 16 >= combo.fontMetrics().horizontalAdvance(combo.currentText()), (
                    combo.currentText()
                )
            assert p.exposure.isVisibleTo(p) and p.mode_menu.isVisibleTo(p)
            for row in p.toolbar_rows:
                for group, *_ in row.groups:
                    for button in group.findChildren(QtWidgets.QToolButton):
                        if button.isVisibleTo(p):
                            rect = QtCore.QRect(button.mapTo(row, QtCore.QPoint()), button.size())
                            assert abs(rect.center().y() - row.rect().center().y()) <= 1, (
                                button.accessibleName(),
                                rect,
                                row.rect(),
                            )
        p.grab().save(str(dest / f"viewer-{width}.png"))
        captures.append(
            dict(width=width, toolbar_heights=heights, font=QtGui.QFontInfo(p.render_button.font()).family())
        )
    p.cancel()
    wait(lambda: p.entry.session.status()["state"] == "cancelled")
    assert p.render_button.isEnabled() and not p.stop_button.isEnabled() and not p.pause_button.isEnabled()
    report = dict(
        result="passed",
        generation=generation,
        geometry_conversions=conversions,
        captures=captures,
        display=viewer_display.describe(p.canvas),
        checks=[
            "name-only dropdowns, full path tooltips, duplicate names and live repairs",
            "Ctrl-middle reset",
            "full-width popup",
            "output-folder click and real Save EXR directory tracking",
            "native render",
            "RGB display isolation",
            "snapshot wipe",
            "display reset",
            "persistent render mode",
            "minimum width keeps all controls visible",
        ],
    )
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    p.dispose()
    p.close()
    registry.close_all()
