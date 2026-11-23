# SPDX-License-Identifier: Apache-2.0
"""One-viewer artist workflow: stock pig, native assignment entry, deletion/repair."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
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


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.obj = hou.node("/obj").createNode("geo", "artist_pig", run_init_scripts=False)
        self.pig = self.obj.createNode("testgeometry_pighead")
        self.assignment = self.obj.createNode("attribwrangle", "material_assignment")
        self.assignment.setInput(0, self.pig)
        self.assignment.parm("class").set(1)
        self.assignment.addSpareParmTuple(hou.StringParmTemplate("material", "Material", 1))
        self.assignment.parm("snippet").set(
            'string path = chs("material"); if (path != "") s@shop_materialpath = path;'
        )
        self.assignment.setRenderFlag(True)
        self.assignment.setDisplayFlag(True)
        self.normal_edit = self.obj.createNode("attribwrangle", "optional_normal_edit")
        self.normal_edit.setInput(0, self.assignment)
        self.normal_edit.parm("class").set(3)
        self.normal_edit.addSpareParmTuple(hou.ToggleParmTemplate("broken_normals", "Zero Normals"))
        self.normal_edit.parm("snippet").set('if (chi("broken_normals")) v@N = set(0,0,0);')
        self.normal_edit.setRenderFlag(True)
        self.normal_edit.setDisplayFlag(True)
        camera = hou.node("/obj").createNode("cam", "artist_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.camera = camera
        self.rop = hou.node("/out").createNode("cycles_ipr", "artist_recovery")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=camera.path(),
                resx=192,
                resy=144,
                samples=4,
                ipr_samples=4,
                allowmotionblur=0,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.valid = self.make_material()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 760))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.stage = "mount-panel"
        self.generation = 0
        self.next_at = 0
        self.started = time.monotonic()
        self.report = dict(
            result="running",
            events=[],
            scope="One viewer; Render/Stop clicks; native parameter typing; camera edit; material delete/recreate",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def make_material(self):
        material = ch.create_material(name="artist_recovery_material", shader="emission")
        material.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
        material.node("surface_shader").parm("strength").set(1)
        return material

    def ready(self, warning, magenta=None):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        if entry.error or status["state"] == "failed":
            raise RuntimeError(entry.error or status["error"])
        info = entry.session.frame_info()
        if not info or not info["complete"] or info["generation"] <= self.generation:
            return False
        if self.panel._live_generation != info["generation"]:
            return False
        issues = status["source_diagnostics"]
        assert bool(issues) == warning, status
        raw = entry.session.frame()
        pixels = np.frombuffer(raw["planes"]["combined"]["pixels"], np.float32).reshape(144, 192, 4)
        if magenta is None:
            magenta = warning
        if magenta:
            marker = (pixels[..., 0] > 0.8) & (pixels[..., 1] < 0.1) & (pixels[..., 2] > 0.8)
        else:
            marker = (pixels[..., 1] > 0.6) & (pixels[..., 0] < 0.2)
        assert marker.sum() > 1000, (self.stage, int(marker.sum()))
        assert not self.panel.canvas.image.isNull()
        assert len(registry.entries()) == 1 and entry.views == 1
        assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        self.report["events"].append(dict(stage=self.stage, status=status))
        self.generation = info["generation"]
        return True

    def tick(self):
        try:
            if time.monotonic() - self.started > 100:
                raise TimeoutError(self.stage + ": " + (self.panel.info.text() if self.panel else "No panel widget"))
            if time.monotonic() < self.next_at:
                return
            if self.stage == "mount-panel":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "stock-pig"
            elif self.stage == "stock-pig":
                if not self.ready(True):
                    return
                assert "source warning" in self.panel.info.text()
                assert "principledshader" in self.panel.info.toolTip()
                self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(700, 650))
                self.settings.setCurrentNode(self.assignment)
                self.settings.setPin(True)
                window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
                window.show()
                window.raise_()
                window.activateWindow()
                self.settings.setIsCurrentTab()
                self.settings.moveFocusTo("material")
                self.stage = "native-entry"
                self.next_at = time.monotonic() + 0.5
            elif self.stage == "native-entry":
                focus = QtWidgets.QApplication.focusWidget() or hou.qt.floatingPanelWindow(
                    self.settings.floatingPanel()
                )
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.valid.path())
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.stage = "assigned"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "assigned":
                assert self.assignment.evalParm("material") == self.valid.path()
                if not self.ready(False):
                    return
                self.valid.destroy()
                self.stage = "deleted"
            elif self.stage == "deleted":
                if not self.ready(True):
                    return
                self.valid = self.make_material()
                self.stage = "recreated"
            elif self.stage == "recreated":
                if not self.ready(False):
                    return
                self.camera.parm("tx").set(0.15)
                self.stage = "camera"
            elif self.stage == "camera":
                if not self.ready(False):
                    return
                self.normal_edit.parm("broken_normals").set(True)
                self.stage = "invalid-normals"
            elif self.stage == "invalid-normals":
                if not self.ready(True, magenta=False):
                    return
                issues = self.panel.entry.session.status()["source_diagnostics"]
                assert len(issues) == 1 and issues[0]["code"] == "geometry.invalid_normal", issues
                assert "normals" in self.panel.info.toolTip()
                self.normal_edit.parm("broken_normals").set(False)
                self.stage = "repaired-normals"
            elif self.stage == "repaired-normals":
                if not self.ready(False):
                    return
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.stage = "stopped"
                self.next_at = time.monotonic() + 0.5
            elif self.stage == "stopped":
                assert self.panel.render_button.isEnabled() and not self.panel.stop_button.isEnabled()
                self.finish(None)
                return
            self.path.write_text(json.dumps(self.report, indent=2))
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error):
        hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
