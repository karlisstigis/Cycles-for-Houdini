# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: the render viewer's chrome size follows the monitor it is on, in every display mode.

The viewer's own rule (cycles_houdini.viewer.ui_scale.MonitorScale): with
Houdini's Qt device-pixel scaling off (device pixel ratio 1 on every screen),
the screen under the panel's centre gives a logical DPI; the factor is that
DPI over the primary screen's logical DPI, the viewer font is the application
font scaled by it, and every chrome length is the font pixel size / 12. So a
button stays the same physical size on a 4K 150 % monitor and an HD 100 %
monitor, independent of Houdini's own UI scale. For classic, native and gpu
display modes, docked and floating (and the floating window moved to every
screen, hidden and shown, reloaded) this records the viewer row height,
render button, status font, MonitorScale's factor, the screen of the panel's
window and of the canvas's own window, and Houdini's own UI scale (information
only), then asserts the viewer sizes equal the rule for the screen the panel
is on. Writes the GUI report and one CHROME_SCALE_REPORT stderr line; never saves.
"""

import json
import os
from pathlib import Path
import sys
import time
import traceback

import hou
from PySide6 import QtCore, QtGui, QtWidgets

try:
    from cycles_houdini.viewer.viewer_display import ENVIRONMENT as DISPLAY_ENVIRONMENT
except ImportError:  # Before the display modes existed: classic only.
    DISPLAY_ENVIRONMENT = "CYCLES_HOUDINI_VIEWER_DISPLAY"

# -RunName <name>__display_<mode> (tests/gui_startup/bootstrap.py) measures that mode only.
MODES = (
    [os.environ["CYCLES_HOUDINI_VIEWER_DISPLAY"]]
    if os.environ.get("CYCLES_HOUDINI_VIEWER_DISPLAY")
    else ["classic", "native", "gpu"]
)
SETTLE = 0.8


def screen_info(screen):
    if screen is None:
        return None
    return dict(name=screen.name(), dpr=screen.devicePixelRatio(), dpi=round(screen.logicalDotsPerInch(), 3))


def window_screen(widget):
    handle = widget.windowHandle() if widget is not None else None
    return screen_info(handle.screen()) if handle is not None else None


def expected_for(panel):
    app = QtWidgets.QApplication.instance()
    screen = app.screenAt(panel.mapToGlobal(panel.rect().center())) or panel.screen()
    primary = app.primaryScreen()
    factor = screen.logicalDotsPerInch() / primary.logicalDotsPerInch()
    font = app.font()
    if font.pixelSize() > 0:
        pixels = max(1, round(font.pixelSize() * factor))
    else:
        pixels = max(1, round(font.pointSizeF() * screen.logicalDotsPerInch() / 72))
    chrome = pixels / 12
    # Qt can replace QScreen objects (display changes): keep plain values only.
    return screen_info(screen), factor, pixels, chrome


def houdini_scale():
    info = {}
    try:
        info["globalScaleFactor"] = hou.ui.globalScaleFactor()
    except Exception as error:
        info["globalScaleFactor"] = "unavailable: " + str(error)
    try:
        info["scaledSize_24"] = hou.ui.scaledSize(24)
    except Exception as error:
        info["scaledSize_24"] = "unavailable: " + str(error)
    info["HOUDINI_UISCALE"] = os.environ.get("HOUDINI_UISCALE")
    info["QT_ENABLE_HIGHDPI_SCALING"] = os.environ.get("QT_ENABLE_HIGHDPI_SCALING")
    info["QT_SCALE_FACTOR"] = os.environ.get("QT_SCALE_FACTOR")
    font = QtWidgets.QApplication.font()
    info["app_font"] = dict(pixel=font.pixelSize(), point=font.pointSizeF(), family=font.family())
    main_font = hou.qt.mainWindow().font()
    info["main_window_font"] = dict(pixel=main_font.pixelSize(), point=main_font.pointSizeF())
    info["main_window_font_pixels"] = QtGui.QFontInfo(main_font).pixelSize()
    return info


def artist_prefs():
    """Only the UI scale lines of the artist's normal houdini.env (information)."""
    lines = []
    for base in (Path.home() / "Documents", Path.home() / "OneDrive" / "Documents"):
        path = base / "houdini22.0" / "houdini.env"
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "SCALE" in line.upper() and not line.lstrip().startswith("#"):
                    lines.append(line.strip())
            return dict(path=str(path), scale_lines=lines)
    return dict(path=None, scale_lines=lines)


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        app = QtWidgets.QApplication.instance()
        self.report = dict(
            result="running",
            screens=[
                dict(
                    screen_info(s),
                    geometry=[s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height()],
                )
                for s in app.screens()
            ],
            primary=app.primaryScreen().name(),
            houdini=houdini_scale(),
            artist_prefs=artist_prefs(),
            samples=[],
            failures=[],
            notes=[],
        )
        self.desktop = hou.ui.curDesktop()
        self.tab = None
        self.standalone = None
        self.other = None
        self.steps = []
        for mode in MODES:
            self.steps += [
                ("mount", "docked", mode),
                ("measure", "docked", mode, "first show"),
                ("hide", "docked", mode),
                ("show", "docked", mode),
                ("measure", "docked", mode, "shown again"),
                ("close", "docked", mode),
                ("mount", "floating", mode),
            ]
            names = [s.name() for s in app.screens()]
            for name in names + [names[0]]:
                self.steps += [("move", "floating", mode, name), ("measure", "floating", mode, "on " + name)]
            if len(names) > 1:
                self.steps += [
                    ("move", "floating", mode, names[1]),
                    ("measure", "floating", mode, "on " + names[1] + " before reload"),
                    ("reload", "floating", mode),
                    ("measure", "floating", mode, "reloaded on " + names[1]),
                    ("minimize", "floating", mode),
                    ("restore", "floating", mode),
                    ("measure", "floating", mode, "restored on " + names[1]),
                    ("fullscreen", "floating", mode),
                    ("measure_fullscreen", "floating", mode, "image-only fullscreen on " + names[1]),
                    ("unfullscreen", "floating", mode),
                    ("move", "floating", mode, names[0]),
                    ("measure", "floating", mode, "back on " + names[0]),
                ]
            self.steps.append(("close", "floating", mode))
            # Several GUI tests show the viewer as its own top-level window.
            self.steps += [("mount", "standalone", mode), ("measure", "standalone", mode, "first show")]
            for name in names[1:] + names[:1]:
                self.steps += [("move", "standalone", mode, name), ("measure", "standalone", mode, "on " + name)]
            self.steps.append(("close", "standalone", mode))
        # houdini_viewer_redesign_smoke.py (a hython test) sets a 12 px
        # application font; run inside Houdini it changes Houdini's font.
        self.steps += [
            ("app_font", "standalone", "classic"),
            ("mount", "standalone", "classic"),
            ("measure", "standalone", "classic", "12 px application font"),
            ("close", "standalone", "classic"),
            ("restore_font", "standalone", "classic"),
        ]
        self.due = time.monotonic() + 1.0
        self.started = time.monotonic()
        self.finished = False
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    @property
    def panel(self):
        if getattr(self, "standalone", None) is not None:
            return self.standalone
        if self.tab is None:
            return None
        try:
            widget = self.tab.activeInterfaceRootWidget()
        except hou.Error:
            return None
        if widget is None or getattr(widget, "_disposed", True):
            return None
        return widget

    def floating(self):
        for floating in hou.ui.floatingPanels():
            for pane in floating.paneTabs():
                if pane == self.tab:
                    return floating
        return None

    def tick(self):
        if self.finished:
            return
        try:
            if time.monotonic() - self.started > 240:
                raise TimeoutError("probe exceeded 240 s at " + repr(self.steps[:1]))
            if time.monotonic() < self.due:
                return
            if not self.steps:
                self.finish()
                return
            step = self.steps[0]
            if (
                step[0] not in ("mount", "close", "show", "unfullscreen", "reload", "app_font", "restore_font")
                and self.panel is None
            ):
                return  # A (re)created viewer is not ready yet.
            done = getattr(self, "do_" + step[0])(*step[1:])
            if done is not False:
                self.steps.pop(0)
                self.due = time.monotonic() + (SETTLE if step[0] not in ("measure", "measure_fullscreen") else 0.05)
            self.save()
        except Exception:
            self.finish(traceback.format_exc())

    # Steps ---------------------------------------------------------------
    def do_mount(self, layout, mode):
        os.environ[DISPLAY_ENVIRONMENT] = mode
        if layout == "standalone":
            from cycles_houdini.panel import RenderPanel

            self.standalone = RenderPanel()
            self.standalone.resize(1100, 700)
            self.standalone.show()
            return
        if layout == "docked":
            viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
            pane = viewer.pane()
            self.tab = pane.createTab(hou.paneTabType.PythonPanel)
        else:
            self.tab = self.desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 700))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.tab.setIsCurrentTab()

    def do_app_font(self, layout, mode):
        app = QtWidgets.QApplication.instance()
        self.saved_font = QtGui.QFont(app.font())
        font = QtGui.QFont("Segoe UI")
        font.setPixelSize(12)
        app.setFont(font)

    def do_restore_font(self, layout, mode):
        QtWidgets.QApplication.instance().setFont(self.saved_font)

    def do_hide(self, layout, mode):
        self.other = self.tab.pane().createTab(hou.paneTabType.Textport)
        self.other.setIsCurrentTab()

    def do_show(self, layout, mode):
        self.tab.setIsCurrentTab()
        if self.other is not None:
            self.other.close()
            self.other = None

    def do_close(self, layout, mode):
        if layout == "standalone":
            self.standalone.dispose()
            self.standalone.close()
            self.standalone.deleteLater()
            self.standalone = None
            return
        floating = self.floating() if layout == "floating" else None
        (floating or self.tab).close()
        self.tab = None

    def do_move(self, layout, mode, name):
        screens = QtWidgets.QApplication.screens()
        destination = next(s for s in screens if s.name() == name)
        rect = destination.availableGeometry()
        if layout == "standalone":
            self.standalone.move(rect.x() + 60, rect.y() + 60)
            return
        floating = self.floating()
        width, height = min(1100, rect.width() - 80), min(700, rect.height() - 80)
        bottom = max(s.geometry().bottom() + 1 for s in screens)
        floating.setSize((width, height))
        floating.setPosition((rect.x() + 40, bottom - rect.y() - 40 - height))

    def do_reload(self, layout, mode):
        self.tab.reloadActiveInterface()

    def do_minimize(self, layout, mode):
        window = hou.qt.floatingPanelWindow(self.floating())
        window.showMinimized()

    def do_restore(self, layout, mode):
        window = hou.qt.floatingPanelWindow(self.floating())
        window.showNormal()

    def do_fullscreen(self, layout, mode):
        self.panel.fullscreen_button.click()

    def do_unfullscreen(self, layout, mode):
        panel = self.panel
        if panel is not None:
            panel.close_fullscreen()

    def do_measure_fullscreen(self, layout, mode, label):
        panel = self.panel
        sample = dict(
            mode=mode,
            layout=layout,
            label=label,
            canvas_window=window_screen(panel.canvas),
            canvas_top_level=type(panel.canvas.window()).__name__,
        )
        # The image-only window keeps the viewer's toolbars; record their size.
        sample["render_button"] = [panel.render_button.width(), panel.render_button.height()]
        sample["rows"] = [row.height() for row in panel.toolbar_rows]
        sample["fullscreen_window"] = window_screen(panel._fullscreen) if panel._fullscreen is not None else None
        sample["chrome_scale"] = panel.chrome_scale
        self.report["samples"].append(sample)

    def do_measure(self, layout, mode, label):
        panel = self.panel
        screen, factor, pixels, chrome = expected_for(panel)
        button = panel.render_button
        rows = [row.height() for row in panel.toolbar_rows]
        row_hints = [row.sizeHint().height() for row in panel.toolbar_rows]
        groups = [
            [
                (
                    g.sizeHint().height(),
                    g.height(),
                    type(g.layout().itemAt(0).widget()).__name__ if g.layout().count() else "",
                )
                for g, *_ in row.groups
            ]
            for row in panel.toolbar_rows
        ]
        expected_button = max(1, round(20 * chrome))  # Render button: 20 px at a 12 px font.
        sample = dict(
            mode=mode,
            layout=layout,
            label=label,
            display_mode=getattr(panel.canvas, "display_mode", "classic (pre-modes)"),
            screen_at_centre=screen,
            panel_screen=screen_info(panel.screen()),
            panel_window=window_screen(panel.window()),
            panel_window_class=type(panel.window()).__name__,
            canvas_window=window_screen(panel.canvas) if panel.canvas.internalWinId() else None,
            canvas_dpr=panel.canvas.devicePixelRatioF(),
            panel_dpr=panel.devicePixelRatioF(),
            factor=panel.monitor_scale.factor,
            reference_dpi=panel.monitor_scale.reference_dpi,
            chrome_scale=getattr(panel, "chrome_scale", None),
            rows=rows,
            row_hints=row_hints,
            groups=groups,
            margins=[row.row.contentsMargins().top() for row in panel.toolbar_rows],
            render_button=[button.width(), button.height()],
            icon=[button.iconSize().width(), button.iconSize().height()],
            button_font_px=button.font().pixelSize(),
            status_font_px=QtGui.QFontInfo(panel.info.font()).pixelSize(),
            status_height=panel.info.height(),
            button_mm=round(button.height() / screen["dpi"] * 25.4 / screen["dpr"], 2),
            expected=dict(factor=factor, font_px=pixels, chrome_scale=chrome, button=expected_button),
            houdini_scaled_24=self.report["houdini"]["scaledSize_24"],
            timer_active=panel.timer.isActive(),
            lifetime=getattr(getattr(panel, "_lifetime", None), "state", None),
        )
        self.report["samples"].append(sample)
        problems = []
        if abs(panel.monitor_scale.factor - factor) > 1e-9:
            problems.append(("factor", panel.monitor_scale.factor, factor))
        if button.font().pixelSize() != pixels:
            problems.append(("button font", button.font().pixelSize(), pixels))
        if button.height() != expected_button:
            problems.append(("button height", button.height(), expected_button))
        if rows != row_hints:
            problems.append(("row heights", rows, row_hints))
        for row in panel.toolbar_rows:
            # A row never outgrows its controls (panel_navigation_checks.check_layout).
            margins = row.row.contentsMargins()
            tallest = max(group.sizeHint().height() for group, *_ in row.groups)
            if row.height() > tallest + margins.top() + margins.bottom() + 2:
                problems.append(("row taller than its controls", row.height(), tallest))
        if problems:
            self.report["failures"].append(dict(mode=mode, layout=layout, label=label, problems=problems))

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        try:
            if self.standalone is not None:
                self.standalone.dispose()
                self.standalone.close()
            if self.tab is not None:
                (self.floating() or self.tab).close()
        except Exception:
            self.report["notes"].append("cleanup: " + traceback.format_exc(limit=2))
        if error is None and self.report["failures"]:
            error = "viewer chrome does not follow its monitor rule: " + json.dumps(self.report["failures"])[:2000]
        self.report["result"] = "failed" if error else "passed"
        self.report["error"] = error
        self.save()
        sys.__stderr__.write(
            "CHROME_SCALE_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n"
        )
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


probe = Probe()
