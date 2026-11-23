# SPDX-License-Identifier: Apache-2.0
"""Native live COP shutter/color/world repairs with one artist viewer."""

import json, os, time, traceback
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry
from cycles_houdini.materials import create_world_material
from cop_shutter_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.f
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 550))
        self.settings.setCurrentNode(f.source)
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
            scope="Native parameter keyboard edits and source topology edits, one retained IPR worker/viewer, accepted image metadata and independent raw planes. Physical-screen and production latency certification are separate.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def edit(self, parm, value):
        self.parm = parm
        self.text = str(value)
        self.settings.setCurrentNode(parm.node())
        self.settings.setIsCurrentTab()
        for folder, index in zip(parm.containingFolderSetParmTuples(), parm.containingFolderIndices()):
            template = parm.node().parmTemplateGroup().find(folder.name())
            folder[0].set(
                1
                if isinstance(template, hou.FolderParmTemplate) and template.folderType() == hou.folderType.Collapsible
                else index
            )
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus(QtCore.Qt.OtherFocusReason)
        QtGui.QCursor.setPos(self.window.mapToGlobal(self.window.rect().center()))
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.25

    def mutate(self):
        f = self.f
        if self.stage in (1, 2, 4, 5, 6, 7):
            self.edit(f.problem, {1: 1, 2: 0, 4: 1, 5: 0, 6: 2, 7: 0}[self.stage])
            return
        if self.stage == 3:
            f.choose("modern")
        elif self.stage == 8:
            self.environment = f.modern.createNode("constant", "world")
            self.environment.parm("signature").set("f3")
            self.environment.parmTuple("f3").set((0.125, 0.25, 0.5))
            self.environment.addSpareParmTuple(
                hou.FloatParmTemplate("red_input", "Red Input", 1, default_value=(0.125,))
            )
            self.environment.parm("f3r").setExpression('ch("red_input")', hou.exprLanguage.Hscript)
            self.world = create_world_material(name="live_cop_world", texture="op:" + self.environment.path())
            self.image = self.world.node("environment_texture")
            self.image.parm("colorspace").set("data")
            f.rop.setParms(dict(world_source="material", world_material=self.world.path()))
        elif self.stage == 9:
            self.edit(self.environment.parm("red_input"), 0.625)
            return
        elif self.stage == 10:
            self.image.parm("filename").set("op:/img/live_missing_world/source")
        elif self.stage == 11:
            network = hou.node("/img").createNode("copnet", "live_missing_world")
            network.setParms(dict(setres=1, res1=16, res2=8))
            self.replacement = network.createNode("constant", "source")
            self.replacement.parm("signature").set("f3")
            self.replacement.parmTuple("f3").set((0.375, 0.5, 0.25))
        elif self.stage == 12:
            self.world.node("background_shader").parm("strength").setExpression("2+$T*12", hou.exprLanguage.Hscript)
        elif self.stage == 13:
            self.edit(hou.node(f.rop.evalParm("camera")).parm("tx"), 0.05)
            return
        self.phase = "wait"
        self.next_at = time.monotonic() + 0.2

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 220, (self.stage, self.phase, self.report)
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
                assert abs(self.parm.eval() - float(self.text)) < 1e-6, (self.parm.path(), self.parm.eval(), self.text)
                self.phase = "wait"
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            frame = entry.session.frame_info()
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
            raw = entry.session.frame()
            planes = {
                name: np.frombuffer(p["pixels"], np.float32).reshape(90, 160, p["channels"]).copy()
                for name, p in raw["planes"].items()
            }
            assert all(np.isfinite(a).all() for a in planes.values()) and planes["combined"][:, :, :3].max() > 0.1
            issues = status["source_diagnostics"]
            assert bool(issues) == (self.stage in (1, 4, 6, 10, 12, 13)), issues
            if self.stage in (0, 3):
                self.base = planes
                self.rebuilt = status["displacement_rebuilds"]
            if self.stage in (1, 4):
                assert np.mean(np.abs(self.base["combined"] - planes["combined"])) > 0.0001
            if self.stage in (2, 5, 7):
                assert np.allclose(self.base["position"], planes["position"], atol=1e-5)
            if self.stage == 5:
                self.rebuilt = status["displacement_rebuilds"]
            if self.stage == 6:
                assert status["displacement_rebuilds"] == self.rebuilt and np.allclose(
                    self.base["position"], planes["position"], atol=1e-5
                )
            corner = (
                (0, 0, 0)
                if self.stage < 8 or self.stage == 10
                else {
                    8: (0.125, 0.25, 0.5),
                    9: (0.625, 0.25, 0.5),
                    11: (0.375, 0.5, 0.25),
                    12: (0.75, 1, 0.5),
                    13: (0.75, 1, 0.5),
                }[self.stage]
            )
            assert np.allclose(planes["combined"][2, 2, :3], corner, atol=2e-4, rtol=0), (
                self.stage,
                planes["combined"][2, 2],
                corner,
                issues,
            )
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
                    diagnostics=issues,
                    corner=planes["combined"][2, 2, :3].tolist(),
                    geometry_conversions=status["geometry_conversions"],
                )
            )
            self.path.write_text(json.dumps(self.report, indent=2))
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
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
