import pass_fixtures as pass_fixture

# SPDX-License-Identifier: Apache-2.0
from cycles_houdini.device_selection import set_devices as set_rop_devices
import hou, os, json, time, traceback
from pathlib import Path
from PySide6 import QtCore, QtTest, QtWidgets, QtGui
from cycles_houdini.panel import RenderPanel
from cycles_houdini import registry, native_scene
from gui_shutdown import retire_and_exit


class Test:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.stage = -1
        self.started = time.monotonic()
        self.pick_generation = 0
        geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
        box = geo.createNode("box")
        box.parmTuple("size").set((4, 4, 0.01))
        self.camera = hou.node("/obj").createNode("cam")
        pass_fixture.set_parameters(self.camera, dict(tz=5, focus=2))
        self.rop = hou.node("/out").createNode("cycles_ipr")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="sop",
                soppath=box.path(),
                camera=self.camera.path(),
                resx=160,
                resy=120,
                ipr_samples=8,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                pixel_aspect=1.5,
                pass_depth=0,
                pass_position=0,
            ),
        )
        self.viewport = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer).curViewport()
        self.viewport.setCamera(self.camera)
        self.viewport.lockCameraToView(True)
        self.panel = RenderPanel()
        self.panel.resize(1100, 750)
        self.panel.show()
        self.panel.set_rop(self.rop)
        self.panel.auto.setChecked(False)
        assert self.panel.rop_path.currentText() == self.rop.name()
        assert self.panel.rop_path.toolTip() == self.rop.path()
        assert self.panel.camera.currentText() == self.camera.name()
        assert self.panel.camera.toolTip() == self.camera.path()
        # Actual combo popup/key selection updates the ROP's camera.
        alt = hou.node("/obj").createNode("cam", "viewer_menu_alternate")
        pass_fixture.parm(alt, "tz").set(8)
        self.panel.camera.showPopup()
        QtTest.QTest.keyClick(self.panel.camera, QtCore.Qt.Key_End)
        QtTest.QTest.keyClick(self.panel.camera, QtCore.Qt.Key_Return)
        assert pass_fixture.evaluate(self.rop, "camera") == alt.path(), self.panel.camera.currentData()
        self.panel.camera.showPopup()
        QtTest.QTest.keyClick(self.panel.camera, QtCore.Qt.Key_Home)
        QtTest.QTest.keyClick(self.panel.camera, QtCore.Qt.Key_Return)
        assert pass_fixture.evaluate(self.rop, "camera") == self.camera.path()
        for field in (self.panel.exposure, self.panel.gamma):
            field.setValue(2)
            target = next(iter(field.input.findChildren(QtWidgets.QLineEdit)), field.input)
            QtTest.QTest.mouseClick(target, QtCore.Qt.MiddleButton, QtCore.Qt.ControlModifier)
            assert field.value() == field.default
        QtTest.QTest.mouseClick(self.panel.camera_lock, QtCore.Qt.LeftButton)
        assert not self.viewport.isCameraLockedToView()
        self.transform = self.camera.worldTransform()
        view = self.viewport.defaultCamera().stash()
        view.setTranslation(hou.Vector3(view.translation()) + hou.Vector3(1, 0, 0))
        self.viewport.setDefaultCamera(view)
        assert self.camera.worldTransform() == self.transform
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        hou.ui.addEventLoopCallback(self.tick)

    def tick(self):
        try:
            if time.monotonic() - self.started > 50:
                raise TimeoutError(self.panel.info.text())
            entry = self.panel.entry
            if not entry:
                return
            if entry.error:
                raise RuntimeError(entry.error)
            frame = entry.session.frame_info()
            if not frame or not frame["complete"] or self.panel._live_generation != frame["generation"]:
                return
            if self.stage == -1:
                self.pick_generation = frame["generation"]
                self.stage = 0
                self.focus_toggled = time.monotonic()
                # Arming, disarming and rearming are tool operations, even with Auto off.
                for _ in range(3):
                    QtTest.QTest.mouseClick(self.panel.focus_pick, QtCore.Qt.LeftButton)
                assert self.panel.canvas.cursor().shape() == QtCore.Qt.CrossCursor
                QtTest.QTest.keyPress(self.panel.canvas, QtCore.Qt.Key_Space)
                assert self.panel.canvas.cursor().shape() == QtCore.Qt.OpenHandCursor
                QtTest.QTest.keyRelease(self.panel.canvas, QtCore.Qt.Key_Space)
                assert self.panel.canvas.cursor().shape() == QtCore.Qt.CrossCursor
                QtTest.QTest.keyClick(self.panel.canvas, QtCore.Qt.Key_Escape)
                assert not self.panel.focus_pick.isChecked() and not self.panel.canvas.focus_enabled
                assert self.panel.canvas.cursor().shape() != QtCore.Qt.CrossCursor
                QtTest.QTest.mouseClick(self.panel.focus_pick, QtCore.Qt.LeftButton)
                return
            if self.stage == 0:
                if time.monotonic() - self.focus_toggled < 0.75:
                    return
                assert frame["generation"] == self.pick_generation, "Focus toggle restarted rendering"
                assert {"position", "depth"} <= set(frame["passes"])
                if not entry.session.focus_sample(80, 60):
                    return
                self.generation = frame["generation"]
                self.stage = 1
                # Off-axis click with non-square pixels: the focus plane is still z=4.995.
                rect = self.panel.canvas.image_rect()
                point = QtCore.QPoint(int(rect.left() + rect.width() * 0.6), int(rect.top() + rect.height() * 0.45))
                QtTest.QTest.mouseClick(self.panel.canvas, QtCore.Qt.LeftButton, pos=point)
                assert pass_fixture.parm(self.camera, "cycles_focus_distance"), self.panel.info.text()
                assert abs(pass_fixture.evaluate(self.camera, "cycles_focus_distance") - 4.995) < 0.01, (
                    self.panel.info.text()
                )
                assert pass_fixture.evaluate(self.camera, "cycles_dof") == 1
                assert (
                    pass_fixture.evaluate(self.rop, "pass_depth") == 0
                    and pass_fixture.evaluate(self.rop, "pass_position") == 0
                )
            elif self.stage == 2 or frame["generation"] > self.generation:
                assert native_scene.capture(self.rop)["camera_dof"]["aperture_size"] > 0
                if self.stage == 1:
                    self.panel.canvas.set_zoom(100)
                    self.panel.canvas._pan = QtCore.QPointF(17, -9)
                    QtTest.QTest.mouseClick(self.panel.keep_image_size, QtCore.Qt.LeftButton)
                    self.locked_rect = self.panel.canvas.image_rect()
                    self.locked_size = self.panel.canvas.full_size
                    self.generation = frame["generation"]
                    self.stage = 2
                    editor = self.panel.preview_resolution.lineEdit()
                    editor.setFocus()
                    editor.selectAll()
                    QtTest.QTest.keyClicks(editor, "50%")
                    QtTest.QTest.keyClick(editor, QtCore.Qt.Key_Return)
                    assert self.panel.preview_resolution.currentText() == "50%"
                    QtTest.QTest.mouseClick(self.panel.refresh_button, QtCore.Qt.LeftButton)
                    return
                if self.panel.canvas.full_size[0] != self.locked_size[0] // 2:
                    return
                assert self.panel.canvas.image_rect() == self.locked_rect
                assert (
                    abs(
                        self.panel.canvas.view_scale()
                        / self.panel.canvas.resolution_scale()
                        * self.panel.canvas.devicePixelRatioF()
                        - 1
                    )
                    < 1e-8
                )
                # Pan and box zoom must use the compensated pixel scale, including after
                # the renderer recreates its buffers at the new resolution.
                canvas = self.panel.canvas
                original_generation = self.panel.entry.session.status()["generation"]
                start = canvas.image_rect().center().toPoint()
                delta = QtCore.QPoint(23, -11)
                QtTest.QTest.mousePress(canvas, QtCore.Qt.MiddleButton, pos=start)
                QtTest.QTest.mouseMove(canvas, start + delta)
                QtTest.QTest.mouseRelease(canvas, QtCore.Qt.MiddleButton, pos=start + delta)
                moved = canvas.image_rect()
                assert moved.size() == self.locked_rect.size()
                assert (moved.topLeft() - self.locked_rect.topLeft() - QtCore.QPointF(delta)).manhattanLength() < 1e-8
                center = moved.center().toPoint()
                point0 = center - QtCore.QPoint(25, 20)
                point1 = center + QtCore.QPoint(25, 20)
                expected = min(canvas.width() / 50, canvas.height() / 40)
                QtTest.QTest.mousePress(canvas, QtCore.Qt.RightButton, QtCore.Qt.ControlModifier, pos=point0)
                QtTest.QTest.mouseMove(canvas, point1)
                QtTest.QTest.mouseRelease(canvas, QtCore.Qt.RightButton, QtCore.Qt.ControlModifier, pos=point1)
                assert abs(canvas.image_rect().width() / moved.width() - expected) < 1e-8
                self.panel.canvas.set_zoom(100)
                self.panel.canvas.zoom_at(1.5, self.locked_rect.center())
                assert abs(self.panel.canvas.image_rect().width() / self.locked_rect.width() - 1.5) < 1e-8
                QtTest.QTest.mouseClick(self.panel.keep_image_size, QtCore.Qt.LeftButton)
                self.panel.canvas.set_zoom(100)
                assert abs(self.panel.canvas.image_rect().width() / self.locked_rect.width() - 0.5) < 1e-8
                self.panel.canvas.fit_image()
                assert self.panel.entry.session.status()["generation"] == original_generation
                assert self.panel.fit_button.toolButtonStyle() == QtCore.Qt.ToolButtonIconOnly
                for button in (self.panel.render_button, self.panel.fit_button, self.panel.diagnostics_menu):
                    assert not button.icon().isNull() and button.accessibleName()
                assert self.panel.display.isVisible() and self.panel.view.isVisible()
                assert self.panel.display.count() > 0 and self.panel.view.count() > 0
                assert self.panel.display.mapTo(self.panel, QtCore.QPoint()).y() > self.panel.canvas.y()
                action = self.panel.diagnostics_menu.menu().actions()[0]
                assert self.panel.memory_details.isChecked()
                action.trigger()
                assert not self.panel.memory_details.isChecked()
                action.trigger()
                assert self.panel.memory_details.isChecked()
                # A compact toolbar must remain bounded at different monitor/font scales.
                row_height = max(
                    max(control.height(), control.sizeHint().height())
                    for control in (
                        self.panel.rop_path,
                        self.panel.render_button,
                        self.panel.pass_menu,
                        self.panel.fit_button,
                    )
                )
                margins = self.panel.layout().contentsMargins()
                toolbar_limit = 5 * row_height + 2 * self.panel.layout().spacing() + margins.top() + margins.bottom()
                assert self.panel.canvas.y() <= toolbar_limit, (self.panel.canvas.y(), row_height, toolbar_limit)
                editor = self.panel.preview_resolution.lineEdit()
                margins = editor.textMargins()
                assert (
                    editor.contentsRect().width() - margins.left() - margins.right() - 4
                    >= editor.fontMetrics().horizontalAdvance(editor.text())
                ), (
                    editor.geometry(),
                    editor.contentsRect(),
                    editor.text(),
                    self.panel.preview_resolution.size(),
                    self.panel.preview_resolution.font().toString(),
                    self.panel.chrome_scale,
                )
                # The render/focus assertions above use actual renderer pixels. Substitute
                # the supplied picture only for the final toolbar layout screenshot.
                picture = (
                    Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
                    / "UI_mockup/Render viewer \u2014 redesign-html/assets/3ea1af0f8add3291bf3811ab56cc9f6c.jpg"
                )
                if picture.exists():
                    image = QtGui.QImage(str(picture))
                    self.panel.canvas.image = image
                    self.panel.canvas.full_size = (image.width(), image.height())
                    self.panel.canvas.fit_image()
                self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.finish(
                    dict(
                        result="passed",
                        focus=pass_fixture.evaluate(self.camera, "cycles_focus_distance"),
                        checks=[
                            "camera dropdown keyboard selection",
                            "native numeric Ctrl-middle reset",
                            "all toolbar controls visible",
                            "viewport navigation leaves camera unchanged",
                            "focus crosshair, Space pan cursor and Escape cancel without restarting",
                            "focus tool on/off does not restart rendering",
                            "image focus click updates render with Auto off",
                            "off-axis focus distance with pixel aspect",
                            "viewer-only picking passes",
                            "real render with camera DOF",
                            "resolution change preserves image size and zoom",
                            "pan and box zoom at compensated resolution",
                            "display controls do not restart rendering",
                            "bottom color controls and diagnostics popup",
                        ],
                    )
                )
        except Exception:
            self.finish(dict(result="failed", error=traceback.format_exc()))

    def finish(self, result):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        entry = self.panel.entry
        retire_and_exit(
            self.path,
            result,
            [entry.session] if entry else [],
            0 if result["result"] == "passed" else 1,
            (self.panel.dispose, self.panel.close, registry.close_all),
        )


test = Test()
