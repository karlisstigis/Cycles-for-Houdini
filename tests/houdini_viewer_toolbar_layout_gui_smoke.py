# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: the viewer's top two toolbar rows share one control height; dicing sits after Denoise.

For classic, native and gpu display modes, docked, this measures the two
toolbar rows (Render/ROP/camera row and AOV/channels/exposure row): row
heights, every visible control's height and vertical centre, and the
cluster wrappers of Render, Denoise and the subdivision dicing buttons
(Adaptive Subdivision Update, Refresh Tessellation). It asserts equal row
heights, equal control heights in both rows, vertical centring, the dicing
cluster placed between the Denoise cluster and the ROP dropdown, no divider
lines between controls or groups, and the
dicing cluster in the same wrapper (class, kind, size, glyph size) as the
Render and Denoise clusters. Every joined (transport) cluster is a segmented
control: its first button carries the left-corner property and its last the
right-corner property (Show Noisy lost it in a14e963). Region, Denoise and Clay
are icon-only squares of the other buttons' size, with accessible names and
tooltips that name them. Geometry only, no screenshots. Writes the GUI
report and one TOOLBAR_LAYOUT_REPORT stderr line; never saves.
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

MODES = [os.environ[DISPLAY_ENVIRONMENT]] if os.environ.get(DISPLAY_ENVIRONMENT) else ["classic", "native", "gpu"]
CONTROLS = (QtWidgets.QToolButton, QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QLineEdit)


def box(widget, panel):
    origin = widget.mapTo(panel, QtCore.QPoint(0, 0))
    return [origin.x(), origin.y(), widget.width(), widget.height()]


def controls(row, panel):
    result = []
    for widget in row.findChildren(QtWidgets.QWidget):
        if not isinstance(widget, CONTROLS) or not widget.isVisibleTo(panel):
            continue
        if isinstance(widget.parentWidget(), CONTROLS):
            continue  # A combo's or spin box's own editor.
        if widget.parentWidget() is not None and widget.parentWidget().objectName() == "qt_spinbox_lineedit":
            continue
        result.append(
            dict(
                name=widget.accessibleName() or widget.toolTip()[:30] or type(widget).__name__,
                cls=type(widget).__name__,
                box=box(widget, panel),
                centre=widget.mapTo(panel, QtCore.QPoint(0, 0)).y() * 2 + widget.height(),
            )
        )
    return result


def wrapper(widget):
    frame = widget.parentWidget()
    return frame


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", samples=[], failures=[])
        self.desktop = hou.ui.curDesktop()
        self.tab = None
        self.steps = []
        for mode in MODES:
            self.steps += [("mount", mode), ("measure", mode), ("close", mode)]
        self.started = time.monotonic()
        self.due = time.monotonic() + 1.0
        self.finished = False
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
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
            if time.monotonic() - self.started > 120:
                raise TimeoutError("probe exceeded 120 s at " + repr(self.steps[:1]))
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
            self.due = time.monotonic() + 1.0
            self.save()
        except Exception:
            self.finish(traceback.format_exc())

    def do_mount(self, mode):
        os.environ[DISPLAY_ENVIRONMENT] = mode
        viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
        self.tab = viewer.pane().createTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.tab.setIsCurrentTab()

    def do_close(self, mode):
        self.tab.close()
        self.tab = None

    def fail(self, mode, what, *values):
        self.report["failures"].append(dict(mode=mode, problem=what, values=values))

    def do_measure(self, mode):
        p = self.panel
        rows = p.toolbar_rows[:2]
        sample = dict(
            mode=mode,
            chrome_scale=p.chrome_scale,
            rows=[box(row, p) for row in rows],
            controls=[controls(row, p) for row in rows],
        )
        render, denoise, dicing = wrapper(p.render_button), wrapper(p.preview_denoise), wrapper(p.tessellation_update)
        rop = wrapper(p.rop_path)
        frames = dict(render=render, denoise=denoise, dicing=dicing, rop=rop)
        sample["wrappers"] = {
            name: dict(
                cls=type(frame).__name__,
                kind=frame.property("cycles_cluster"),
                compact=frame.property("cycles_compact"),
                box=box(frame, p),
            )
            for name, frame in frames.items()
        }
        sample["buttons"] = {
            name: dict(box=box(button, p), icon=[button.iconSize().width(), button.iconSize().height()])
            for name, button in (
                ("render", p.render_button),
                ("denoise", p.preview_denoise),
                ("show_noisy", p.show_noisy),
                ("dicing_update", p.tessellation_update),
                ("dicing_refresh", p.tessellation_refresh),
            )
        }
        self.report["samples"].append(sample)
        # No vertical divider lines between controls or groups anywhere in the chrome.
        dividers = [
            w.metaObject().className()
            for w in p.findChildren(QtWidgets.QWidget)
            if w.property("cycles_separator")
            or (isinstance(w, QtWidgets.QFrame) and w.frameShape() == QtWidgets.QFrame.VLine)
        ]
        if dividers:
            self.fail(mode, "divider lines in the chrome", dividers)
        for row in p.all_toolbar_rows:
            for group, *_ in row.groups:
                if group.layout().count() == 0:
                    self.fail(mode, "empty toolbar group")
        heights = [row.height() for row in rows]
        if heights[0] != heights[1]:
            self.fail(mode, "row heights differ", heights)
        tallest = [max(item["box"][3] for item in row) for row in sample["controls"]]
        if tallest[0] != tallest[1]:
            self.fail(mode, "tallest control differs between rows", tallest)
        for index, row in enumerate(sample["controls"]):
            centre = rows[index].mapTo(p, QtCore.QPoint(0, 0)).y() * 2 + rows[index].height()
            off = [item for item in row if abs(item["centre"] - centre) > 2]
            if off:
                self.fail(mode, "controls not vertically centred in row %d" % index, off)
        b = sample["wrappers"]
        if not (b["denoise"]["box"][0] + b["denoise"]["box"][2] <= b["dicing"]["box"][0] < b["rop"]["box"][0]):
            self.fail(mode, "dicing cluster not between Denoise and the ROP dropdown", b)
        for key in ("cls", "kind", "compact"):
            values = {b[name][key] for name in ("render", "denoise", "dicing")}
            if len(values) != 1:
                self.fail(mode, "wrappers differ in " + key, values)
        if len({b[name]["box"][3] for name in ("render", "denoise", "dicing")}) != 1:
            self.fail(mode, "wrapper heights differ", b)
        buttons = sample["buttons"]
        if len({tuple(v["icon"]) for v in buttons.values()}) != 1 or len({v["box"][3] for v in buttons.values()}) != 1:
            self.fail(mode, "button heights or glyph sizes differ", buttons)
        for frame in p.findChildren(QtWidgets.QWidget):
            if frame.property("cycles_cluster") != "transport" or not frame.isVisibleTo(p):
                continue
            parts = sorted(
                (
                    w
                    for w in frame.findChildren(QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly)
                    if w.isVisibleTo(p)
                ),
                key=lambda w: w.x(),
            )
            first, last = parts[0], parts[-1]
            if not (first.property("cycles_transport_start") or first.property("cycles_primary")):
                self.fail(mode, "joined group's first button has square left corners", first.accessibleName())
            if not last.property("cycles_transport_end"):
                self.fail(mode, "joined group's last button has square right corners", last.accessibleName())
        square = p.show_noisy.size()
        for control, name in ((p.region, "Region"), (p.preview_denoise, "Denoise"), (p.clay, "Clay")):
            problems = []
            if control.text() or control.icon().isNull() or control.toolButtonStyle() != QtCore.Qt.ToolButtonIconOnly:
                problems.append("not icon-only")
            if control.size() != square:
                problems.append(f"size {control.width()}x{control.height()} differs from Show Noisy")
            if not control.accessibleName() or not control.toolTip().startswith(name + ": "):
                problems.append("accessible name or tooltip missing its name")
            if not control.isCheckable():
                problems.append("not checkable")
            if problems:
                self.fail(mode, name + ": " + ", ".join(problems), control.toolTip())

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        try:
            if self.tab is not None:
                self.tab.close()
        except Exception:
            pass
        if error is None and self.report["failures"]:
            error = "toolbar layout: " + json.dumps(self.report["failures"], default=str)[:2000]
        self.report["result"] = "failed" if error else "passed"
        self.report["error"] = error
        self.save()
        sys.__stderr__.write(
            "TOOLBAR_LAYOUT_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n"
        )
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


probe = Probe()
