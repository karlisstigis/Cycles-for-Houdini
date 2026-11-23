# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: the viewer's image canvas stays in its slot through any pane layout change.

In native and gpu display modes the viewer root is the one native window in
Houdini's pane (as Houdini's RE_Window) and the canvas is an ordinary child
inside it. After each pane layout change (split fraction, synthetic splitter
drags, pane maximise/restore, tab switch, desktop switch, image-only
fullscreen in/out, floating window move/resize, main window move) and after
the fast cases (tight loops of split changes without event processing,
bursts with partial processing, rapid 300-move drags released at once,
repeated maximise toggles; measured once the event queue drains and again
0.5 s later) this checks that the canvas widget fills its slot between the
toolbar rows and that every native window (the viewer root, and the canvas
when it has one) is exactly over its widget on screen (QWindow.mapToGlobal,
which asks the platform window), in device pixels. Writes the GUI report and
one NATIVE_GEOMETRY_REPORT stderr line; never saves.
"""

import json
import os
from pathlib import Path
import sys
import time
import traceback

import hou
from PySide6 import QtCore, QtWidgets
from cycles_houdini.viewer.viewer_display import ENVIRONMENT as DISPLAY_ENVIRONMENT

MODES = [os.environ[DISPLAY_ENVIRONMENT]] if os.environ.get(DISPLAY_ENVIRONMENT) else ["native", "gpu"]
SETTLE = 0.6


def rect(r):
    return [r.x(), r.y(), r.width(), r.height()]


def native_rects(widget):
    """(widget rectangle, its native window's rectangle) on screen, in device pixels."""
    ratio = widget.devicePixelRatioF()
    origin = widget.mapToGlobal(QtCore.QPoint(0, 0))
    expected = [round(v * ratio) for v in (origin.x(), origin.y(), widget.width(), widget.height())]
    handle = widget.windowHandle()
    if handle is None or not widget.internalWinId():
        return expected, None, None
    window_origin = handle.mapToGlobal(QtCore.QPoint(0, 0))
    actual = [round(v * ratio) for v in (window_origin.x(), window_origin.y(), handle.width(), handle.height())]
    native_parent = widget.nativeParentWidget()
    detail = dict(
        handle_geometry=rect(handle.geometry()),
        handle_parent=type(handle.parent()).__name__ if handle.parent() is not None else None,
        native_parent=native_parent.metaObject().className() if native_parent is not None else None,
        in_native_parent=[
            widget.mapTo(native_parent, QtCore.QPoint(0, 0)).x(),
            widget.mapTo(native_parent, QtCore.QPoint(0, 0)).y(),
        ]
        if native_parent is not None
        else None,
        visible=handle.isVisible(),
    )
    return expected, actual, detail


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", samples=[], failures=[], notes=[], skipped=[])
        self.desktop = hou.ui.curDesktop()
        self.main = hou.qt.mainWindow()
        self.tab = self.split = self.other = None
        self.steps = []
        for mode in MODES:
            self.steps += [
                ("mount_docked", mode),
                ("measure", mode, "docked"),
                ("fraction", mode, 0.25),
                ("measure", mode, "split 0.25"),
                ("fraction", mode, 0.7),
                ("measure", mode, "split 0.7"),
                ("fraction", mode, 0.45),
                ("measure", mode, "split 0.45"),
                ("drag", mode, -300),
                ("measure", mode, "splitter dragged left"),
                ("drag", mode, 450),
                ("measure", mode, "splitter dragged right"),
                # The artist's fast case: many layout changes before the event queue drains.
                ("fast", mode, "tight splitter loop"),
                ("measure", mode, "held after tight splitter loop"),
                ("fast", mode, "splitter bursts"),
                ("measure", mode, "held after splitter bursts"),
                ("fast", mode, "rapid drag"),
                ("measure", mode, "held after rapid drag"),
                ("fast", mode, "rapid drag back"),
                ("measure", mode, "held after rapid drag back"),
                ("fast", mode, "paced drag"),
                ("measure", mode, "held after paced drag"),
                ("fast", mode, "paced drag back"),
                ("measure", mode, "held after paced drag back"),
                ("fast", mode, "maximise toggles"),
                ("measure", mode, "held after maximise toggles"),
                ("direction", mode, "Vertical"),
                ("measure", mode, "split vertical"),
                ("direction", mode, "Horizontal"),
                ("measure", mode, "split horizontal"),
                ("maximize", mode, True),
                ("measure", mode, "pane maximised"),
                ("maximize", mode, False),
                ("measure", mode, "pane restored"),
                ("other_tab", mode, True),
                ("other_tab", mode, False),
                ("measure", mode, "tab shown again"),
                ("fullscreen", mode, True),
                ("measure", mode, "image-only fullscreen"),
                ("fullscreen", mode, False),
                ("measure", mode, "fullscreen closed"),
                ("main_move", mode, True),
                ("measure", mode, "main window moved"),
                ("main_move", mode, False),
                ("measure", mode, "main window back"),
                ("desktop", mode, True),
                ("desktop", mode, False),
                ("measure", mode, "desktop shown again"),
                ("fraction", mode, 0.55),
                ("measure", mode, "split 0.55 after desktop"),
                ("close", mode),
                ("mount_floating", mode),
                ("measure", mode, "floating"),
                ("floating_geometry", mode, (900, 600), 200),
                ("measure", mode, "floating resized and moved"),
                ("floating_geometry", mode, (1300, 800), -150),
                ("measure", mode, "floating resized and moved back"),
                ("close", mode),
            ]
        self.started = time.monotonic()
        self.due = time.monotonic() + 1.0
        self.finished = False
        self.timer = QtCore.QTimer(self.main)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    @property
    def panel(self):
        try:
            widget = self.tab.activeInterfaceRootWidget() if self.tab is not None else None
        except hou.Error:
            return None
        return None if widget is None or getattr(widget, "_disposed", True) else widget

    def tick(self):
        if self.finished:
            return
        try:
            if time.monotonic() - self.started > 300:
                raise TimeoutError("probe exceeded 300 s at " + repr(self.steps[:1]))
            if time.monotonic() < self.due:
                return
            if not self.steps:
                self.finish()
                return
            step = self.steps[0]
            if step[0] == "measure" and self.panel is None:
                return
            getattr(self, "do_" + step[0])(*step[1:])
            self.steps.pop(0)
            # A fast step measures once drained; the next measure, 0.5 s later, checks it holds.
            self.due = time.monotonic() + (0.05 if step[0] == "measure" else 0.5 if step[0] == "fast" else SETTLE)
            self.save()
        except Exception:
            self.finish(traceback.format_exc())

    def do_mount_docked(self, mode):
        os.environ[DISPLAY_ENVIRONMENT] = mode
        viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
        self.split = viewer.pane()
        pane = self.split.splitHorizontally()
        self.tab = pane.tabs()[0].setType(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.tab.setIsCurrentTab()

    def do_mount_floating(self, mode):
        os.environ[DISPLAY_ENVIRONMENT] = mode
        self.tab = self.desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 700))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))

    def do_fraction(self, mode, fraction):
        self.split.setSplitFraction(fraction)

    def do_direction(self, mode, name):
        try:
            self.split.setSplitDirection(getattr(hou.paneSplitDirection, name))
        except Exception as error:
            self.report["skipped"].append("split direction " + name + ": " + str(error))

    def do_drag(self, mode, distance):
        """Drag Houdini's pane splitter left of the viewer with (synthetic) mouse events."""
        from PySide6 import QtTest

        panel = self.panel
        origin = panel.mapToGlobal(QtCore.QPoint(0, 0))
        start = QtCore.QPoint(origin.x() - 3, origin.y() + panel.height() // 2)
        target = QtWidgets.QApplication.widgetAt(start)
        if target is None:
            self.report["skipped"].append("splitter drag: no widget at " + str((start.x(), start.y())))
            return
        local = target.mapFromGlobal(start)
        self.report["notes"].append(dict(drag_target=target.metaObject().className(), at=[start.x(), start.y()]))
        QtTest.QTest.mousePress(target, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, local)
        steps = 12
        for index in range(1, steps + 1):
            point = local + QtCore.QPoint(round(distance * index / steps), 0)
            QtTest.QTest.mouseMove(target, point, 15)
            QtWidgets.QApplication.processEvents()
            if index == steps // 2:
                self.do_measure(mode, "during splitter drag " + str(distance))
        QtTest.QTest.mouseRelease(
            target, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, local + QtCore.QPoint(distance, 0)
        )

    def drain(self):
        app = QtWidgets.QApplication.instance()
        for _ in range(50):
            app.processEvents()
            app.sendPostedEvents()
        app.processEvents()

    def do_fast(self, mode, kind):
        from PySide6 import QtTest

        app = QtWidgets.QApplication.instance()
        if kind == "tight splitter loop":
            for index in range(60):
                self.split.setSplitFraction(0.3 if index % 2 else 0.7)
            self.split.setSplitFraction(0.42)
        elif kind == "splitter bursts":
            for index in range(60):
                self.split.setSplitFraction(0.25 + 0.5 * ((index * 7) % 11) / 10)
                if index % 4 == 0:
                    app.processEvents(QtCore.QEventLoop.AllEvents, 1)
            self.split.setSplitFraction(0.58)
        elif kind.startswith(("rapid drag", "paced drag")):
            panel = self.panel
            origin = panel.mapToGlobal(QtCore.QPoint(0, 0))
            start = QtCore.QPoint(origin.x() - 3, origin.y() + panel.height() // 2)
            target = QtWidgets.QApplication.widgetAt(start)
            if target is None:
                self.report["skipped"].append(kind + ": no widget at the splitter")
                return
            local = target.mapFromGlobal(start)
            distance = 380 if kind.endswith("back") else -420
            paced = kind.startswith("paced")
            QtTest.QTest.mousePress(target, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, local)
            for index in range(1, 301):
                # Back and forth like a hand, ending at the full distance.
                fraction = index / 300 if not paced else min(1.0, abs(((index * 4 / 300) % 2) - 1) + index / 300)
                QtTest.QTest.mouseMove(target, local + QtCore.QPoint(round(distance * fraction), (index % 3) - 1), 0)
                if paced:
                    app.processEvents(QtCore.QEventLoop.AllEvents, 2)  # Mouse moves at about 500 Hz.
                    time.sleep(0.002)
                elif index % 50 == 0:
                    app.processEvents(QtCore.QEventLoop.AllEvents, 1)
            QtTest.QTest.mouseRelease(
                target, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, local + QtCore.QPoint(distance, 0)
            )
        elif kind == "maximise toggles":
            pane = self.tab.pane()
            for index in range(12):
                pane.setIsMaximized(index % 2 == 0)
                if index % 3 == 0:
                    app.processEvents(QtCore.QEventLoop.AllEvents, 1)
            pane.setIsMaximized(False)
        self.drain()
        self.do_measure(mode, "drained after " + kind)

    def do_maximize(self, mode, value):
        self.tab.pane().setIsMaximized(value)

    def do_other_tab(self, mode, show):
        if show:
            self.other = self.tab.pane().createTab(hou.paneTabType.Textport)
            self.other.setIsCurrentTab()
        else:
            self.tab.setIsCurrentTab()
            self.other.close()
            self.other = None

    def do_fullscreen(self, mode, value):
        panel = self.panel
        if value:
            panel.open_fullscreen()
        else:
            panel.close_fullscreen()

    def do_main_move(self, mode, away):
        if away:
            self.main_state = (self.main.isMaximized(), self.main.geometry())
            if self.main.isMaximized():
                self.main.showNormal()
            geometry = self.main.screen().availableGeometry()
            self.main.setGeometry(
                geometry.x() + 120, geometry.y() + 90, geometry.width() - 400, geometry.height() - 300
            )
        else:
            maximized, geometry = self.main_state
            self.main.setGeometry(geometry)
            if maximized:
                self.main.showMaximized()

    def do_desktop(self, mode, away):
        if away:
            others = [d for d in hou.ui.desktops() if d.name() != self.desktop.name()]
            if not others:
                self.report["skipped"].append("desktop switch: only one desktop")
                return
            others[0].setAsCurrent()
        else:
            self.desktop.setAsCurrent()

    def floating(self):
        for floating in hou.ui.floatingPanels():
            if any(pane == self.tab for pane in floating.paneTabs()):
                return floating
        return None

    def do_floating_geometry(self, mode, size, shift):
        floating = self.floating()
        x, y = floating.position()
        floating.setSize(tuple(int(v) for v in size))
        floating.setPosition((int(x) + shift, int(y) + shift // 2))

    def do_close(self, mode):
        if self.split is not None and self.tab is not None and self.floating() is None:
            self.tab.pane().close() if hasattr(self.tab.pane(), "close") else self.tab.close()
        else:
            (self.floating() or self.tab).close()
        self.tab = self.split = None

    def do_measure(self, mode, label):
        panel = self.panel
        canvas = panel.canvas
        expected, actual, detail = native_rects(canvas)
        sample = dict(mode=mode, label=label, canvas=expected, window=actual, detail=detail)
        if panel.internalWinId() and panel.height() > 0:  # A 0-height widget keeps its hidden window.
            panel_expected, panel_actual, panel_detail = native_rects(panel)
            sample["panel_native"] = dict(widget=panel_expected, window=panel_actual)
            if panel_actual != panel_expected:
                self.report["failures"].append(
                    dict(
                        mode=mode,
                        label=label,
                        problem="panel window not over panel",
                        widget=panel_expected,
                        window=panel_actual,
                    )
                )
        overlays = []
        fullscreen = getattr(panel, "_fullscreen", None)
        if fullscreen is not None:
            for overlay in (fullscreen.top, fullscreen.bottom):
                fullscreen.reveal(overlay)
                QtWidgets.QApplication.processEvents()
                overlay_expected, overlay_actual, _ = native_rects(overlay)
                overlays.append(dict(widget=overlay_expected, window=overlay_actual))
                if overlay_actual is not None and overlay_actual != overlay_expected:
                    self.report["failures"].append(
                        dict(
                            mode=mode,
                            label=label + " (fullscreen toolbar)",
                            widget=overlay_expected,
                            window=overlay_actual,
                        )
                    )
            fullscreen.hide_controls()
        sample["overlays"] = overlays
        self.report["samples"].append(sample)
        # The canvas widget itself spans the panel's width inside the panel.
        top_left = canvas.mapTo(panel, QtCore.QPoint(0, 0)) if canvas.window() is panel.window() else None
        if top_left is not None and (
            top_left.x() != 0
            or canvas.width() != panel.width()
            or top_left.y() < 0
            or top_left.y() + canvas.height() > panel.height()
        ):
            self.report["failures"].append(
                dict(
                    mode=mode,
                    label=label,
                    problem="canvas widget outside the panel",
                    in_panel=[top_left.x(), top_left.y()],
                    panel=[panel.width(), panel.height()],
                    canvas=[canvas.width(), canvas.height()],
                )
            )
        sample["in_panel"] = [top_left.x(), top_left.y()] if top_left is not None else None
        if top_left is not None:
            # The slot between the image toolbar row and the colour row, from the rows themselves.
            above, below = panel.all_toolbar_rows[1], panel.all_toolbar_rows[2]
            top = above.mapTo(panel, QtCore.QPoint(0, 0)).y() + above.height()
            slot = [0, top, panel.width(), below.mapTo(panel, QtCore.QPoint(0, 0)).y() - top]
            sample["slot"] = slot
            if [top_left.x(), top_left.y(), canvas.width(), canvas.height()] != slot:
                self.report["failures"].append(
                    dict(
                        mode=mode,
                        label=label,
                        problem="canvas not in its slot",
                        in_panel=[top_left.x(), top_left.y(), canvas.width(), canvas.height()],
                        slot=slot,
                    )
                )
        if actual is None:
            if not panel.internalWinId():
                self.report["failures"].append(dict(mode=mode, label=label, problem="viewer has no native window"))
        elif actual != expected:
            self.report["failures"].append(dict(mode=mode, label=label, widget=expected, window=actual))

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        if error is None and self.report["failures"]:
            error = "canvas native window not over its widget: " + json.dumps(self.report["failures"])[:2000]
        self.report["result"] = "failed" if error else "passed"
        self.report["error"] = error
        self.save()
        sys.__stderr__.write(
            "NATIVE_GEOMETRY_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n"
        )
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


probe = Probe()
