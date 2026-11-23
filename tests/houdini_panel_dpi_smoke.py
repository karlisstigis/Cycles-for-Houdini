# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: move the actual floating Python Panel across available screens."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import sys
import time
import traceback
import gc
from unittest.mock import patch
import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini.ui_scale import logical_scale, scaled_style


class ScreenWorkflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", host_pid=os.getpid(), screens=[], samples=[], toolbar_states=[])
        self.checking_controls = False
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.floating = self.tab.floatingPanel()
        self.window = hou.qt.floatingPanelWindow(self.floating)
        screens = QtWidgets.QApplication.screens()
        for screen in screens:
            rect = screen.availableGeometry()
            self.report["screens"].append(
                dict(
                    name=screen.name(),
                    geometry=[rect.x(), rect.y(), rect.width(), rect.height()],
                    ratio=screen.devicePixelRatio(),
                    logical_dpi=screen.logicalDotsPerInch(),
                    physical_dpi=screen.physicalDotsPerInch(),
                )
            )
        self.index = -1
        names = [screen.name() for screen in screens]
        self.visits = [*names, *reversed(names)]
        self.app_font = QtWidgets.QApplication.font().key()
        self.app_style = QtWidgets.QApplication.instance().styleSheet()
        self.host_font = hou.qt.mainWindow().font().key()
        for dpi, reference, expected in ((96, 144, 2 / 3), (144, 96, 1.5), (120, 96, 1.25), (96, 96, 1)):
            assert abs(logical_scale(dpi, reference) - expected) < 1e-9
        # Already-scaled Qt configurations report the same logical DPI on
        # 1x/1.25x/2x screens. The UI correction stays one (no double scaling).
        assert logical_scale(96, 96) == 1
        assert scaled_style("padding: 6px; image: url(icon16px.png);", 0.5) == "padding: 3px; image: url(icon16px.png);"
        geo = hou.node("/obj").createNode("geo", "monitor_test_geometry")
        sop = geo.createNode("torus", "native_torus")
        sop.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "monitor_test_camera")
        camera.parm("tz").set(6)
        self.rop = hou.node("/out").createNode("cycles_ipr", "monitor_test_render")
        set_rop_devices(self.rop, "CPU")
        self.rop.setParms(
            dict(
                soppath=sop.path(),
                camera=camera.path(),
                resx=320,
                resy=180,
                samples=4,
                ipr_samples=4,
                ipr_resolution_percent=100,
                ipr_use_denoise=0,
            )
        )
        self.panel.auto.setChecked(False)
        self.panel.set_rop(self.rop)
        self.panel.queue_render()
        self.initial_image = None
        self.began = time.monotonic()
        self.due = self.began + 1
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def check_toolbar_states(self):
        # Exercise the rendered controls, with the pointer both over and away
        # from them. A checked-property test alone cannot catch a stale tint.
        self.checking_controls = True
        try:
            captures = []
            # Widget dimensions alone missed the native QToolButton margins:
            # compare the painted centre columns in the complete toolbar too.
            panel = self.panel
            controls = (
                panel.final_render_button,
                panel.render_button,
                panel.pause_button,
                panel.stop_button,
                panel.refresh_button,
            )
            frame = panel.refresh_button.parentWidget()
            assert panel.auto.parentWidget() is frame and frame.property("cycles_cluster") == "transport"
            image = panel.grab().toImage()
            background = panel._viewer_colors["toolbar"]
            painted = []
            for button in controls:
                assert button.width() == button.height() == panel.render_button.height()
                origin = button.mapTo(panel, QtCore.QPoint())
                ys = [
                    origin.y() + y
                    for y in range(button.height())
                    if image.pixelColor(origin.x() + button.width() // 2, origin.y() + y) != background
                ]
                assert ys, (button.accessibleName(), "No visible button background")
                painted.append((min(ys), max(ys)))
            assert len(set(painted)) == 1, painted
            panel.toolbar_rows[0].grab().save(str(self.path.with_suffix(".transport-" + str(self.index) + ".png")))
            self.report.setdefault("transport_bounds", []).append(
                dict(
                    visit=self.index, painted=painted, square_size=panel.render_button.height(), auto_with_refresh=True
                )
            )
            for name, button in (("Fit", self.panel.fit_button), ("Checkers", self.panel.checker)):
                if name == "Fit":
                    # F is Actual pixels (1:1); G now selects the green channel.
                    QtTest.QTest.keyClick(self.panel.canvas, QtCore.Qt.Key_F)
                    assert self.panel.canvas.view_mode == "actual" and not button.isChecked()
                elif button.isChecked():
                    QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
                assert not button.isChecked()
                if name == "Checkers":
                    assert not self.panel.canvas.checker_enabled
                for state in ("off", "hover", "on"):
                    if state == "hover":
                        # Houdini embeds the widget in a native window rather
                        # than a Qt QWindow. Deliver Qt's Enter/Leave explicitly:
                        # QTest mouse movement cannot enter that native proxy.
                        pos = QtCore.QPointF(button.rect().center())
                        global_pos = QtCore.QPointF(button.mapToGlobal(button.rect().center()))
                        QtWidgets.QApplication.sendEvent(button, QtGui.QEnterEvent(pos, pos, global_pos))
                        QtWidgets.QApplication.sendEvent(
                            button, QtGui.QHoverEvent(QtCore.QEvent.HoverEnter, pos, QtCore.QPointF(-1, -1))
                        )
                    else:
                        if state == "on":
                            QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
                            assert button.isChecked()
                            if name == "Fit":
                                assert self.panel.canvas.view_mode == "fit"
                            else:
                                assert self.panel.canvas.checker_enabled
                        QtWidgets.QApplication.sendEvent(button, QtCore.QEvent(QtCore.QEvent.Leave))
                        QtWidgets.QApplication.sendEvent(
                            button,
                            QtGui.QHoverEvent(
                                QtCore.QEvent.HoverLeave, QtCore.QPointF(-1, -1), QtCore.QPointF(button.rect().center())
                            ),
                        )
                        self.panel.canvas.setFocus()
                    QtTest.QTest.qWait(80)
                    assert button.underMouse() == (state == "hover"), (name, state, button.underMouse())
                    image = button.grab().toImage()
                    # One chrome pixel in from the left edge: compact icon
                    # buttons centre a 14 px icon in 20 px, so at 3 px the
                    # sample could land on the icon's edge (seen at scale 1).
                    point = QtCore.QPoint(max(1, round(1 * self.panel.chrome_scale)), image.height() // 2)
                    composite = self.panel.grab().toImage()
                    actual = composite.pixelColor(button.mapTo(self.panel, point))
                    # Fit and Checkers sit in compact transport clusters, whose
                    # buttons rest on the field colour (toolbar elsewhere).
                    resting = "field" if button.parentWidget().property("cycles_cluster") == "transport" else "toolbar"
                    role = {"off": resting, "hover": "hover", "on": "active"}[state]
                    expected = self.panel._viewer_colors[role]
                    difference = sum(abs(a - b) for a, b in zip(actual.getRgb()[:3], expected.getRgb()[:3]))
                    self.report["toolbar_states"].append(
                        dict(
                            visit=self.index,
                            button=name,
                            state=state,
                            checked=button.isChecked(),
                            actual=actual.name(),
                            expected=expected.name(),
                            difference=difference,
                        )
                    )
                    self.save()
                    assert difference <= 12, self.report["toolbar_states"][-1]
                    captures.append((name + " " + state, image))
            sheet = QtGui.QImage(450, 100, QtGui.QImage.Format_RGB32)
            sheet.fill(QtGui.QColor(35, 35, 35))
            painter = QtGui.QPainter(sheet)
            painter.setPen(QtCore.Qt.white)
            for i, (label, image) in enumerate(captures):
                x, y = (i % 3) * 150, (i // 3) * 50
                painter.drawText(x + 4, y + 14, label)
                painter.drawImage(x + 4, y + 18, image)
            painter.end()
            sheet.save(str(self.path.with_suffix(".toolbar-" + str(self.index) + ".png")))
            for start in (-6, -3):
                samples = self.report["toolbar_states"][start : start + 3 if start + 3 else None]
                assert len({sample["actual"] for sample in samples}) == 3, samples
        finally:
            self.panel.canvas.set_zoom(150)
            self.panel.checker.setChecked(True)
            self.checking_controls = False

    def tick(self):
        if self.checking_controls:
            return
        try:
            if time.monotonic() < self.due:
                return
            if time.monotonic() - self.began > 45:
                raise TimeoutError(self.panel.info.text())
            if self.initial_image is None:
                if self.panel.entry is None:
                    return
                status = self.panel.entry.session.status()
                if status["state"] == "failed":
                    raise RuntimeError(status["error"])
                if status["state"] != "complete" or self.panel.canvas.image.isNull():
                    return
                self.initial_image = self.panel.canvas.image.copy()
                self.generation = status["generation"]
                self.panel.canvas.set_zoom(150)
            if self.index >= 0:
                self.window = hou.qt.floatingPanelWindow(self.floating) or self.panel.window()
                position, size = self.floating.position(), self.floating.size()
                origin = self.panel.mapToGlobal(QtCore.QPoint())
                screen = QtWidgets.QApplication.screenAt(origin + self.panel.rect().center())
                assert screen is not None
                screen_name = screen.name()
                assert screen_name == self.visits[self.index], (screen_name, self.visits[self.index])
                factor = logical_scale(screen.logicalDotsPerInch(), self.panel.monitor_scale.reference_dpi)
                self.report["current_ui"] = dict(
                    screen=screen.name(),
                    factor=factor,
                    name=self.panel.objectName(),
                    property=self.panel.property("cycles_render_panel"),
                    style_length=len(self.panel.styleSheet()),
                    root_font=self.panel.font().toString(),
                    button_font=self.panel.render_button.font().toString(),
                )
                self.save()
                assert abs(self.panel.monitor_scale.factor - factor) < 1e-9
                # The chrome follows the monitor it is on, never Houdini's own UI
                # scale (Edit > Preferences > Global UI Size): a different host
                # scale must leave the factor and the control sizes unchanged.
                chrome_font = self.panel.render_button.font().pixelSize()
                chrome_field = self.panel.exposure.input.width()
                host_scale = hou.ui.globalScaleFactor()

                # Change the host scale only as the viewer's scaling module sees
                # it: Houdini's own size APIs (FloatingPanel.setSize) convert
                # through hou.ui.globalScaleFactor and must keep the real one.
                class HostUi:
                    def __getattr__(self, name):
                        return getattr(hou.ui, name)

                    def globalScaleFactor(self):
                        return host_scale * 2 + 0.5

                class Host:
                    ui = HostUi()

                    def __getattr__(self, name):
                        return getattr(hou, name)

                scaling = sys.modules[type(self.panel.monitor_scale).__module__]
                with patch.object(scaling, "hou", Host()):
                    self.panel.monitor_scale.update()
                    QtWidgets.QApplication.processEvents()
                    assert abs(self.panel.monitor_scale.factor - factor) < 1e-9
                    assert self.panel.render_button.font().pixelSize() == chrome_font
                    assert self.panel.exposure.input.width() == chrome_field
                self.panel.monitor_scale.update()
                font = QtWidgets.QApplication.font()
                if font.pixelSize() > 0:
                    assert self.panel.render_button.font().pixelSize() == round(font.pixelSize() * factor), (
                        screen.name(),
                        self.panel.render_button.font().toString(),
                        font.toString(),
                        factor,
                    )
                assert self.panel.exposure.input.width() == round(38 * self.panel.chrome_scale)
                assert self.panel.canvas.image == self.initial_image
                assert self.panel.entry.session.status()["generation"] == self.generation
                assert (
                    abs(self.panel.canvas.image_rect().width() * self.panel.canvas.devicePixelRatioF() - 320 * 1.5)
                    < 1e-6
                )
                assert self.panel.minimumSizeHint().width() < screen.availableGeometry().width()
                assert self.window.width() >= self.panel.minimumWidth(), (
                    self.window.size(),
                    self.panel.minimumWidth(),
                    self.panel.size(),
                    self.floating.size(),
                )
                from cycles_houdini.viewer_theme import read_theme

                # Regression: never retain a Qt wrapper returned through the
                # host's void-pointer color API. Exercise model allocation and
                # collection between real monitor/theme refreshes.
                with patch.object(hou.qt, "getColor", return_value=QtGui.QStandardItem("not a color")):
                    for _ in range(10):
                        self.panel.camera.refresh()
                        gc.collect()
                        theme = read_theme()
                        assert all(isinstance(color, QtGui.QColor) and color.isValid() for color in theme.values())
                        tuple(color.rgba() for color in theme.values())
                        self.panel.monitor_scale.update()
                # The theme is re-read only when Houdini replaces the
                # application palette (its theme-change path), not per poll.
                scale = self.panel.monitor_scale
                scale.update()
                cached = scale._theme
                for _ in range(3):
                    scale.update()
                assert scale._theme is cached, "Theme re-read without a palette change"
                app = QtWidgets.QApplication.instance()
                original = app.palette()
                changed = QtGui.QPalette(original)
                changed.setColor(
                    QtGui.QPalette.ToolTipBase,
                    QtGui.QColor(
                        "#030201" if original.color(QtGui.QPalette.ToolTipBase).name() == "#010203" else "#010203"
                    ),
                )
                try:
                    app.setPalette(changed)
                    scale.update()
                    assert scale._theme is not cached, "Palette change did not refresh the viewer theme"
                finally:
                    app.setPalette(original)
                    scale.update()

                def host_color(name):
                    return QtGui.QColor.fromRgbF(*hou.ui.colorFromName(name).rgb())

                def themed(role):
                    value = QtWidgets.QApplication.instance().property("pluto_" + role)
                    return QtGui.QColor(value) if isinstance(value, QtGui.QColor) and value.isValid() else None

                # Houdini 22 theme roles drive the viewer when present (as for
                # the native style); legacy scheme colors are the fallback.
                def theme_or_legacy(role, legacy):
                    return themed(role) or host_color(legacy)

                assert theme["background"] == theme_or_legacy("bg", "BackColor"), theme["background"].name()
                assert theme["toolbar"] == theme_or_legacy("bg", "PaneHeaderGradLow"), theme["toolbar"].name()
                assert theme["text"] == theme_or_legacy("fg", "TextColor"), theme["text"].name()
                assert theme["field"] == theme_or_legacy("field", "TextboxBG")
                assert theme["dropdown_hi"] == theme_or_legacy("button", "ButtonGradHi")
                assert theme["dropdown_lo"] == theme_or_legacy("button", "ButtonGradLow")
                assert theme["numeric"] == theme_or_legacy("field", "ButtonGradLow")
                image = self.panel.grab().toImage()
                # The preview resolution menu sits in the compact resolution
                # cluster: an embedded (transparent) combo on the compact
                # cluster's toolbar-coloured background, no dropdown gradient.
                frame = self.panel.preview_resolution.parentWidget()
                assert frame is self.panel.resolution_group and frame.property("cycles_cluster") == "transport"
                assert frame.property("cycles_compact")
                menu = self.panel.preview_resolution
                point = menu.mapTo(self.panel, QtCore.QPoint(round(3 * self.panel.chrome_scale), menu.height() // 2))
                actual = image.pixelColor(point)
                difference = sum(abs(a - b) for a, b in zip(actual.getRgb()[:3], theme["toolbar"].getRgb()[:3]))
                assert difference <= 12, (menu.currentText(), actual.name(), theme["toolbar"].name(), difference)
                for menu in (
                    self.panel.rop_path,
                    self.panel.camera,
                    self.panel.pass_menu,
                    self.panel.display,
                    self.panel.view,
                ):
                    frame = menu.parentWidget()
                    assert frame.property("cycles_cluster") == "dropdown"
                    point = frame.mapTo(
                        self.panel, QtCore.QPoint(round(3 * self.panel.chrome_scale), frame.height() // 2)
                    )
                    actual = image.pixelColor(point)
                    expected = [
                        (a + b) / 2
                        for a, b in zip(theme["dropdown_hi"].getRgb()[:3], theme["dropdown_lo"].getRgb()[:3])
                    ]
                    difference = sum(abs(a - b) for a, b in zip(actual.getRgb()[:3], expected))
                    assert difference <= 15, (menu.currentText(), actual.name(), expected, difference)
                for field in (self.panel.exposure, self.panel.gamma):
                    # Sample the label background and the actual native input
                    # interior, away from the value text on the right.
                    frame = field.parentWidget()
                    for widget, x in ((frame, 3), (field.input, 3)):
                        point = widget.mapTo(
                            self.panel, QtCore.QPoint(round(x * self.panel.chrome_scale), widget.height() // 2)
                        )
                        actual = image.pixelColor(point)
                        difference = sum(abs(a - b) for a, b in zip(actual.getRgb()[:3], theme["numeric"].getRgb()[:3]))
                        assert difference <= 12, (
                            widget.metaObject().className(),
                            actual.name(),
                            theme["numeric"].name(),
                            difference,
                        )
                assert theme["active"] == theme_or_legacy("checkedSurface", "ButtonPressedGradLow"), theme[
                    "active"
                ].name()
                assert theme["hover"] != theme["active"]
                from panel_navigation_checks import check_layout

                check_layout(self.panel)
                self.check_toolbar_states()
                # The image-only window must open on the panel's current
                # monitor, independently of native embedding's cached screen.
                self.panel.open_fullscreen()
                fullscreen = self.panel._fullscreen
                assert fullscreen.windowHandle().screen().name() == screen_name
                assert fullscreen.isFullScreen() and self.panel.canvas.window() is fullscreen
                assert self.panel.canvas.image == self.initial_image
                assert self.panel.entry.session.status()["generation"] == self.generation
                QtTest.QTest.keyClick(self.panel.canvas, QtCore.Qt.Key_Escape)
                assert self.panel._fullscreen is None and self.panel.canvas.parentWidget() is self.panel
                assert QtWidgets.QApplication.font().key() == self.app_font
                assert QtWidgets.QApplication.instance().styleSheet() == self.app_style
                assert hou.qt.mainWindow().font().key() == self.host_font
                widgets = (
                    self.panel,
                    self.panel.render_button,
                    self.panel.rop_path,
                    self.panel.exposure.input,
                    self.panel.pass_menu,
                    self.panel.info,
                )
                sample = dict(
                    screen=screen_name,
                    window_ratio=self.window.devicePixelRatioF(),
                    panel_ratio=self.panel.devicePixelRatioF(),
                    window_size=[self.window.width(), self.window.height()],
                    widgets=[],
                    native_position=list(position),
                    qt_position=[origin.x(), origin.y()],
                    host_scale=hou.ui.globalScaleFactor(),
                    qt_inch=hou.qt.inchesToPixels(1),
                )
                for widget in widgets:
                    font = widget.font()
                    sample["widgets"].append(
                        dict(
                            type=widget.metaObject().className(),
                            size=[widget.width(), widget.height()],
                            size_hint=[widget.sizeHint().width(), widget.sizeHint().height()],
                            font_pixels=font.pixelSize(),
                            font_points=font.pointSizeF(),
                            line_spacing=widget.fontMetrics().lineSpacing(),
                            own_style_length=len(widget.styleSheet()),
                        )
                    )
                sample.update(
                    ui_factor=factor,
                    render_generation=self.generation,
                    image_zoom=150,
                    logical_dpi=screen.logicalDotsPerInch(),
                    chrome_font_pixels=self.panel.render_button.font().pixelSize(),
                )
                self.report["samples"].append(sample)
                self.window.grab().save(str(self.path.with_suffix(".screen-" + str(self.index) + ".png")))
                self.save()
            self.index += 1
            if self.index == len(self.visits):
                self.report["result"] = "passed"
                # Native Windows non-client borders change with monitor DPI,
                # so equal requested window sizes can have different content
                # widths. Compare intrinsic control metrics, not stretch width.
                for before, after in zip(self.report["samples"][0]["widgets"], self.report["samples"][-1]["widgets"]):
                    for name in ("font_pixels", "font_points", "line_spacing"):
                        assert before[name] == after[name], (name, before, after)
                assert (
                    self.report["samples"][0]["widgets"][1]["size_hint"]
                    == self.report["samples"][-1]["widgets"][1]["size_hint"]
                )
                assert (
                    self.report["samples"][0]["widgets"][3]["size"] == self.report["samples"][-1]["widgets"][3]["size"]
                )
                # Same physical size on every monitor: logical pixels per logical
                # inch stay constant (Qt's device pixel ratio multiplies both), up
                # to the rounding to whole font pixels.
                first = self.report["samples"][0]
                for sample in self.report["samples"]:
                    expected = first["chrome_font_pixels"] * sample["logical_dpi"] / first["logical_dpi"]
                    assert abs(sample["chrome_font_pixels"] - expected) <= 0.5 + 1e-9, (first, sample)
                self.report["checks"] = [
                    "Each available monitor and return trip",
                    "Destination logical DPI without double device scaling",
                    "Chrome keeps its physical size on each monitor and ignores Houdini's global UI scale",
                    "Native numeric-field width and control fonts",
                    "Unchanged application theme/font",
                    "Unchanged render generation and image",
                    "150% image zoom remains physical-pixel based",
                    "No cumulative font/button/field size changes",
                    "Dropdown button gradients and distinct off/hover/checked pixels match the active Houdini theme on each monitor",
                    "Exposure and gamma wrapper and native editor pixels use the theme numeric background",
                    "Owned theme colors survive model churn and GC without the Qt pointer color API",
                    "Fullscreen opens on each destination monitor and Escape restores the same canvas",
                ]
                self.save()
                self.panel.dispose()
                hou.ui.removeEventLoopCallback(self.callback)
                hou.exit(0, suppress_save_prompt=True)
                return
            screens = QtWidgets.QApplication.screens()
            destination = next((screen for screen in screens if screen.name() == self.visits[self.index]), None)
            assert destination is not None, "Destination monitor disconnected during test"
            rect = destination.availableGeometry()
            # Houdini uses bottom-up desktop Y, Qt uses top-down Y. Keep the
            # entire window inside the destination monitor to prevent the host
            # moving an off-screen window back onto its main monitor.
            width, height = min(1100 if self.index % 2 == 0 else 700, rect.width() - 80), min(760, rect.height() - 80)
            desktop_bottom = max(screen.geometry().bottom() + 1 for screen in screens)
            self.floating.setSize((width, height))
            self.floating.setPosition((rect.x() + 40, desktop_bottom - rect.y() - 40 - height))
            self.due = time.monotonic() + 1.0
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            hou.ui.removeEventLoopCallback(self.callback)
            hou.exit(1, suppress_save_prompt=True)


workflow = ScreenWorkflow()
