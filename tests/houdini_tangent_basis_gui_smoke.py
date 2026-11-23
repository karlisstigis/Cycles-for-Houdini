# SPDX-License-Identifier: Apache-2.0
"""Repair named tangent-basis fields through actual parameter-pane edits."""

import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry
from tangent_basis_fixture import Fixture

CASES = (
    "tangent3",
    "tangent_both",
    "normal_original",
    "normal_displaced",
    "displacement_both",
    "tangent_displacement",
    "shared_normal_displaced",
)


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[])
        self.fixture = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop = self.fixture.rop
        self.report["authoring_controls"] = self.fixture.authoring_controls()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setPin(True)
        self.settings.setCurrentNode(self.fixture.parm.node())
        self.panel = None
        self.stage = "mount"
        self.started = time.monotonic()
        self.next_at = 0
        self.generation = 0
        self.busy = False
        self.index = 0
        self.phase = 0
        self.baseline = None
        example = self.fixture.dest / "tangent-bases.hiplc"
        hou.hipFile.save(str(example))
        hou.ui.addEventLoopCallback(self.tick)

    def begin_edit(self):
        self.settings.setIsCurrentTab()
        window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        window.show()
        window.raise_()
        window.activateWindow()
        self.stage = "focus"
        self.next_at = time.monotonic() + 0.25

    def capture_parameters(self, name):
        window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        # Houdini's native gadgets use a composed surface that QWidget.grab()
        # cannot reliably repaint. Capture this window's displayed pixels.
        pixels = window.screen().grabWindow(window.winId())
        assert not pixels.isNull() and pixels.save(str(self.path.with_name(name)))

    def ready(self):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        assert not entry.error and status["state"] != "failed", (entry.error, status)
        info = entry.session.frame_info()
        if (
            status.get("capture_pending")
            or not info
            or not info["complete"]
            or info["generation"] <= self.generation
            or self.panel._live_generation != info["generation"]
        ):
            return False
        # A generation's first coarse preview is not its completed displayed image.
        if self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]:
            return False
        self.generation = info["generation"]
        broken = self.phase == 1
        pixels = self.fixture.check(entry.session.frame(), status, broken, self.baseline)
        if self.phase == 0:
            self.baseline = pixels
        issues = status["source_diagnostics"]
        assert len(registry.entries()) == 1 and entry.views == 1
        assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        suffix = "world" if self.stage == "controls_wait" else "%d-%d" % (self.index, self.phase)
        filename = "%s-%s.png" % (self.path.stem, suffix)
        self.report["events"].append(
            dict(case=CASES[self.index], broken=broken, generation=self.generation, diagnostics=issues, image=filename)
        )
        assert self.panel.grab().save(str(self.path.with_name(filename)))
        return True

    def tick(self):
        # HOM cooks and Qt keyboard delivery can run nested event loops.
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 300, (self.stage, self.report)
            if time.monotonic() < self.next_at:
                return
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "wait"
            elif self.stage == "focus":
                self.settings.moveFocusTo(self.fixture.parm.name())
                self.stage = "type"
                self.next_at = time.monotonic() + 0.2
            elif self.stage == "type":
                self.stage = "sending"
                focus = QtWidgets.QApplication.focusWidget()
                self.report["input_focus"] = dict(
                    case=CASES[self.index],
                    phase=self.phase,
                    widget=type(focus).__name__,
                    parameter=self.fixture.parm.path(),
                    before=self.fixture.parm.evalAsString(),
                )
                self.capture_parameters("parameter-before.png")
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Backspace)
                QtTest.QTest.keyClicks(focus, "1" if self.phase == 1 else "0")
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.stage = "wait"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "wait":
                assert self.fixture.parm.evalAsInt() == int(self.phase == 1), (
                    self.fixture.parm.evalAsString(),
                    0,
                    self.phase,
                    self.report.get("input_focus"),
                )
                if not self.ready():
                    return
                self.phase += 1
                if self.phase < 3:
                    self.begin_edit()
                else:
                    if self.index + 1 == len(CASES):
                        self.settings.setCurrentNode(self.fixture.source)
                        self.phase = 0
                        self.baseline = None
                        self.stage = "controls_show"
                        self.next_at = time.monotonic() + 0.3
                        return
                    self.index += 1
                    self.fixture.configure(CASES[self.index])
                    self.settings.setCurrentNode(self.fixture.parm.node())
                    self.phase = 0
                    self.baseline = None
                    self.stage = "wait"
                    self.next_at = time.monotonic() + 0.3
            elif self.stage == "controls_show":
                self.fixture.source.updateParmStates()
                assert not self.fixture.map_parm.isDisabled() and not self.fixture.source.parm("base").isDisabled()
                self.capture_parameters("tangent-controls.png")
                self.fixture.source.parm("space").set("world")
                self.fixture.map_parm.set("ignored_missing_map")
                self.fixture.source.updateParmStates()
                assert self.fixture.map_parm.isDisabled() and self.fixture.source.parm("base").isDisabled()
                self.stage = "controls_wait"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "controls_wait":
                if not self.ready():
                    return
                self.report["events"][-1]["case"] = "world_space_ignores_uv_name"
                self.capture_parameters("world-controls.png")
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.finish()
                return
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
