# SPDX-License-Identifier: Apache-2.0
"""One live viewer: native COP parameter edits, deletion/recreation and timeline."""

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
        self.legacy = hou.node("/img").createNode("img", "live_cop_legacy")
        self.color = self.new_color("source", (0.25, 0.5, 0.75, 1))
        self.target = self.legacy.createNode("null", "output")
        self.target.setInput(0, self.color)
        f.texture.setParms(dict(filename="op:" + self.target.path(), colorspace="data", alpha_type="ignore"))
        f.output.setNamedInput("surface", f.texture, "color")
        self.modern = hou.node("/img").createNode("copnet", "live_cop_modern")
        self.modern.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
        self.constant = self.modern.createNode("constant", "source")
        self.constant.parm("signature").set("f3")
        self.constant.parmTuple("f3").set((0.6, 0.2, 0.4))
        self.constant.addSpareParmTuple(hou.FloatParmTemplate("red_input", "Red Input", 1, default_value=(0.6,)))
        self.constant.parm("f3r").setExpression('ch("red_input")', hou.exprLanguage.Hscript)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 500))
        self.settings.setPin(True)
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
            scope="Native parameter-pane keyboard edits and HOM topology/timeline edits, one Cycles Viewer; accepted displayed image metadata and independent pixels, not physical-screen certification.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def new_color(self, name, value):
        node = self.legacy.createNode("color", name)
        node.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
        node.parmTuple("color").set(value)
        node.addSpareParmTuple(hou.FloatParmTemplate("red_input", "Red Input", 1, default_value=(value[0],)))
        node.parm("colorr").setExpression('ch("red_input")', hou.exprLanguage.Hscript)
        return node

    def edit(self, node, parm, value):
        self.parm = node.parm(parm)
        self.pending = str(value)
        self.settings.setCurrentNode(node)
        self.settings.setIsCurrentTab()
        window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        window.show()
        window.raise_()
        window.activateWindow()
        window.setFocus(QtCore.Qt.OtherFocusReason)
        QtGui.QCursor.setPos(window.mapToGlobal(window.rect().center()))
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.25

    def mutate(self):
        f = self.f
        if self.stage == 1:
            self.edit(self.color, "red_input", 0.625)
            return
        elif self.stage == 2:
            self.second = self.new_color("second", (0.125, 0.25, 0.5, 1))
            self.target.setInput(0, self.second)
        elif self.stage == 3:
            self.second.destroy()
        elif self.stage == 4:
            self.second = self.new_color("second", (0.375, 0.5, 0.25, 1))
            self.target.setInput(0, self.second)
        elif self.stage == 5:
            f.texture.parm("filename").set("op:/img/live_cop_repair/source")
        elif self.stage == 6:
            network = hou.node("/img").createNode("img", "live_cop_repair")
            source = network.createNode("color", "source")
            source.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
            source.parmTuple("color").set((0.5, 0.25, 0.125, 1))
        elif self.stage == 7:
            f.texture.parm("filename").set("op:" + self.constant.path())
        elif self.stage == 8:
            self.edit(self.constant, "red_input", 0.8)
            return
        elif self.stage == 9:
            self.constant.parm("f3r").setExpression(".1+$F*.1", hou.exprLanguage.Hscript)
            hou.setFrame(1)
        else:
            hou.setFrame(self.stage - 8)
        self.phase = "wait"
        self.next_at = time.monotonic() + 0.15

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 160, (self.stage, self.phase, self.report)
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
                self.settings.moveFocusTo(self.parm.name())
                self.phase = "keys"
                self.next_at = time.monotonic() + 0.2
                return
            if self.phase == "keys":
                self.phase = "sending"
                focus = QtWidgets.QApplication.focusWidget() or hou.qt.floatingPanelWindow(
                    self.settings.floatingPanel()
                )
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.pending)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = "confirm"
                self.next_at = time.monotonic() + 0.3
                return
            if self.phase == "confirm":
                assert abs(self.parm.eval() - float(self.pending)) < 1e-6, (
                    self.parm.path(),
                    self.parm.eval(),
                    self.pending,
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
            expected = (
                [
                    (0.25, 0.5, 0.75),
                    (0.625, 0.5, 0.75),
                    (0.125, 0.25, 0.5),
                    (1, 0, 1),
                    (0.375, 0.5, 0.25),
                    (1, 0, 1),
                    (0.5, 0.25, 0.125),
                    (0.6, 0.2, 0.4),
                    (0.8, 0.2, 0.4),
                ][self.stage]
                if self.stage < 9
                else (0.1 + (self.stage - 8) * 0.1, 0.2, 0.4)
            )
            measured = self.f.expect(entry.session.frame(), expected)
            assert bool(status["source_diagnostics"]) == (self.stage in (3, 5)), status["source_diagnostics"]
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
                    frame=hou.frame(),
                    generation=self.generation,
                    measured=measured,
                    diagnostics=status["source_diagnostics"],
                    cop=_native.cop_capture_stats(entry.session._handle),
                )
            )
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            self.stage += 1
            if self.stage == 14:
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
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
