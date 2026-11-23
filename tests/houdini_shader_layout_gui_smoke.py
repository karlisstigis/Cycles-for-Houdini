# SPDX-License-Identifier: Apache-2.0
"""Native parameter edits driving operations, Undo and accepted viewer images."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry, _native
from shader_bypass_preview_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.f
        self.vector = f.builder.createNode("cycles::vector_math::1", "adaptive_vector")
        self.vector.parm("scale").set(2)
        # A native published numeric control is keyboard-editable through the
        # host pane. It also exercises operation expressions and dependencies.
        self.vector.addSpareParmTuple(hou.IntParmTemplate("operation_index", "Operation Index", 1, default_value=(0,)))
        self.vector.parm("math_type").setExpression(
            'ifs(ch("operation_index")==1,"scale",ifs(ch("operation_index")==2,"round","multiply"))',
            hou.exprLanguage.Hscript,
        )
        self.vector.setNamedInput("vector1", f.color, "color")
        self.vector.setNamedInput("vector2", f.texture, "color")
        f.texture.parm("filename").set(f.image((0.5, 0.25, 0.125, 1)))
        f.output.setNamedInput("surface", self.vector, "vector")
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 650))
        self.settings.setCurrentNode(self.vector)
        self.network = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(650, 500))
        self.network.setPwd(f.builder)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.stage = 0
        self.phase = "mount"
        self.started = time.monotonic()
        self.next_at = 0
        self.busy = False
        self.generation = 0
        self.report = dict(
            result="running",
            events=[],
            scope="Native numeric parameter keyboard edits driving operation expressions, Undo, one retained worker/viewer, accepted displayed revisions and independent linear pixels; direct dropdown mouse selection and physical-screen certification are separate.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def edit_operation(self, operation):
        self.operation = operation
        self.settings.setCurrentNode(self.vector)
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
        if self.stage in (1, 2, 3, 5):
            self.edit_operation({1: "scale", 2: "round", 3: "multiply", 5: "multiply"}[self.stage])
            return
        if self.stage == 4:
            self.vector.parm("operation_index").set(1)
            self.f.texture.parm("filename").set(str(self.f.dest / "missing-inactive-layout.exr"))
        elif self.stage == 6:
            self.f.texture.parm("filename").set(self.f.image((0.5, 0.25, 0.125, 1)))
        elif self.stage == 7:
            with hou.undos.group("Layout operation test"):
                self.vector.parm("operation_index").set(2)
        elif self.stage == 8:
            hou.undos.performUndo()
        self.phase = "wait"
        self.next_at = time.monotonic() + 0.2

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 165, (self.stage, self.phase, self.report)
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
                self.settings.moveFocusTo("operation_index")
                self.phase = "keys"
                self.next_at = time.monotonic() + 0.2
                return
            if self.phase == "keys":
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, str({"multiply": 0, "scale": 1, "round": 2}[self.operation]))
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = "confirm"
                self.next_at = time.monotonic() + 0.25
                return
            if self.phase == "confirm":
                assert self.vector.evalParm("math_type") == self.operation, (
                    self.vector.evalParm("math_type"),
                    self.operation,
                )
                self.phase = "wait"
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            frame = entry.session.frame_info()
            if (
                status.get("pending_update")
                or status.get("capture_pending")
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
                (0.1, 0.1, 0.1),
                (0.4, 0.8, 1.6),
                (0, 0, 1),
                (0.1, 0.1, 0.1),
                (0.4, 0.8, 1.6),
                None,
                (0.1, 0.1, 0.1),
                (0, 0, 1),
                (0.1, 0.1, 0.1),
            ][self.stage]
            if expected is not None:
                self.f.expect(entry.session.frame(), expected)
            assert bool(status["source_diagnostics"]) == (self.stage == 5), status["source_diagnostics"]
            self.vector.updateParmStates()
            active = _native.vop_input_activity(self.vector.sessionId(), hou.time())
            for name in self.vector.inputNames():
                assert self.vector.isInputVisible(name) == active[name]
                assert self.vector.parmTuple(name).isHidden() == (not active[name])
            assert self.vector.input(1) == self.f.texture and self.vector.evalParm("scale") == 2
            if self.stage == 0:
                self.conversions = status["geometry_conversions"]
                self.pid = status["renderer_pid"]
            assert status["geometry_conversions"] == self.conversions and status["renderer_pid"] == self.pid
            assert status["render_device_ids"] == [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]]
            assert len(registry.entries()) == 1 and entry.views == 1
            self.generation = frame["generation"]
            self.report["events"].append(
                dict(
                    stage=self.stage,
                    operation=self.vector.evalParm("math_type"),
                    generation=self.generation,
                    measured=self.f.measure(entry.session.frame()).tolist(),
                    active=active,
                    drawn=_native.vop_connector_layout(self.vector.sessionId()),
                    diagnostics=status["source_diagnostics"],
                )
            )
            self.path.write_text(json.dumps(self.report, indent=2))
            self.stage += 1
            if self.stage == 9:
                self.finish()
                return
            self.mutate()
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
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
