# SPDX-License-Identifier: Apache-2.0
"""Native pane state, typed edits, wire Undo/Redo and live input precedence."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry
from shader_bypass_preview_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.f
        self.math = f.math
        self.math.parm("math_type").set("add")
        self.math.parm("value1").set(0.125)
        self.math.parm("value2").set(0.5)
        self.source_path = f.value.path()
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setCurrentNode(self.math)
        self.network = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(650, 450))
        self.network.setPwd(f.builder)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.stage = 0
        self.phase = "mount"
        self.started = time.monotonic()
        self.next_at = 0
        self.generation = 0
        self.busy = False
        self.report = dict(
            result="running",
            events=[],
            scope="Native parameter keyboard edits, Houdini wiring/deletion/Undo APIs, real parameter state and one retained viewer with independent linear pixels. Physical wire-drag/pixel presentation certification is separate.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def edit(self, parm, value):
        self.parm = parm
        self.text = str(value)
        # Pane navigation must not insert a selection action between the
        # parameter edit and the wire operation whose Undo we are testing.
        with hou.undos.disabler():
            self.settings.setCurrentNode(parm.node())
            self.settings.setIsCurrentTab()
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus(QtCore.Qt.OtherFocusReason)
        QtGui.QCursor.setPos(self.window.mapToGlobal(self.window.rect().center()))
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.25

    def mutate(self):
        self.report["before_action"] = dict(
            stage=self.stage, undo=list(hou.undos.undoLabels()[:8]), redo=list(hou.undos.redoLabels()[:8])
        )
        if self.stage == 1:
            self.edit(hou.node(self.source_path).parm("value"), 0.6)
            return
        if self.stage == 2:
            with hou.undos.group("Disconnect shader input"):
                self.math.setInput(0, None)
        elif self.stage == 3:
            self.edit(self.math.parm("value1"), 0.25)
            return
        elif self.stage in (4, 5, 9):
            assert hou.undos.performUndo(), self.report["before_action"]
            if self.stage in (5, 9):
                assert self.math.input(0) == hou.node(self.source_path), self.report["before_action"]
        elif self.stage == 6:
            assert hou.undos.performRedo(), self.report["before_action"]
            assert self.math.input(0) is None, self.report["before_action"]
        elif self.stage == 7:
            with hou.undos.group("Reconnect shader input"):
                self.math.setInput(0, hou.node(self.source_path), 0)
        elif self.stage == 8:
            with hou.undos.group("Delete input source"):
                hou.node(self.source_path).destroy()
        elif self.stage == 10:
            self.math.bypass(True)
        elif self.stage == 11:
            self.math.setInput(0, None)
        elif self.stage == 12:
            self.math.bypass(False)
        elif self.stage == 13:
            self.f.emission.parmTuple("color").set((0.4, 0.5, 0.6))
            self.f.emission.setNamedInput("color", self.f.color, "color")
            self.f.output.setNamedInput("surface", self.f.emission, "emission")
            self.settings.setCurrentNode(self.f.emission)
        elif self.stage == 14:
            self.f.emission.setInput(0, None)
        self.phase = "wait"
        self.next_at = time.monotonic() + 0.2

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 170, (self.stage, self.phase, self.report)
            if time.monotonic() < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                return
            if self.phase == "focus":
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "keys"
                self.next_at = time.monotonic() + 0.2
                return
            if self.phase == "keys":
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.text)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = "confirm"
                self.next_at = time.monotonic() + 0.25
                return
            if self.phase == "confirm":
                assert abs(self.parm.eval() - float(self.text)) < 0.00001
                self.phase = "wait"
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            frame = entry.session.frame_info()
            self.report["current"] = dict(
                stage=self.stage,
                status=status,
                frame=frame,
                input=self.math.input(0).path() if self.math.input(0) else None,
                value=self.math.evalParm("value1"),
                undo=list(hou.undos.undoLabels()[:8]),
                redo=list(hou.undos.redoLabels()[:8]),
            )
            self.path.write_text(json.dumps(self.report, indent=2))
            if (
                status.get("capture_pending")
                or status.get("pending_update")
                or not frame
                or not frame["complete"]
                or frame["generation"] <= self.generation
            ):
                return
            if (
                self.panel._live_generation != frame["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != frame["sequence"]
            ):
                return
            expected = [
                (0.8,) * 3,
                (1.1,) * 3,
                (0.625,) * 3,
                (0.75,) * 3,
                (0.625,) * 3,
                (1.1,) * 3,
                (0.625,) * 3,
                (1.1,) * 3,
                (0.625,) * 3,
                (1.1,) * 3,
                (0.6,) * 3,
                (0.125,) * 3,
                (0.625,) * 3,
                (0.2, 0.4, 0.8),
                (0.4, 0.5, 0.6),
            ][self.stage]
            self.f.expect(entry.session.frame(), expected)
            assert not status["source_diagnostics"]
            if self.stage < 13:
                self.math.updateParmStates()
                disabled = self.math.parm("value1").isDisabled()
                assert disabled == (self.math.input(0) is not None), (self.stage, disabled, self.math.inputs())
                assert not self.math.parm("value2").isDisabled()
                assert abs(self.math.evalParm("value1") - (0.25 if self.stage == 3 else 0.125)) < 1e-6
            else:
                self.f.emission.updateParmStates()
                disabled = all(p.isDisabled() for p in self.f.emission.parmTuple("color"))
                assert disabled == (self.stage == 13) and not self.f.emission.parm("strength").isDisabled()
            if self.stage == 0:
                self.pid = status["renderer_pid"]
                self.conversions = status["geometry_conversions"]
            assert status["renderer_pid"] == self.pid and status["geometry_conversions"] == self.conversions
            assert status["render_device_ids"] == [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]]
            assert len(registry.entries()) == 1 and entry.views == 1
            self.generation = frame["generation"]
            self.report["events"].append(
                dict(
                    stage=self.stage,
                    generation=self.generation,
                    disabled=disabled,
                    measured=self.f.measure(entry.session.frame()).tolist(),
                )
            )
            self.path.write_text(json.dumps(self.report, indent=2))
            self.stage += 1
            if self.stage == 15:
                self.finish()
                return
            self.mutate()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
