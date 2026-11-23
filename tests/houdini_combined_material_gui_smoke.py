# SPDX-License-Identifier: Apache-2.0
"""Artist workflow: Render, type native medium density, reconnect its output."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry, create_material


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/materials/combined/surface-volume-example.hiplc"), suppress_save_prompt=True
        )
        self.rop = hou.node("/out/combined_material_render")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(dict(resx=192, resy=144, ipr_samples=32))
        material = hou.node("/mat/surface_and_medium")
        self.medium = material.node("interior_medium")
        self.output = material.node("surface_output")
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 760))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.panel.set_rop(self.rop)
        self.stage = "initial"
        self.generation = 0
        self.started = time.monotonic()
        self.next_at = 0
        self.report = dict(
            result="running",
            events=[],
            scope="One Cycles viewer, native parameter entry, packed closed media, output rewiring and worker retirement.",
        )
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        hou.ui.addEventLoopCallback(self.tick)

    def ready(self, entry, expected, reuse=True):
        if not entry:
            return False
        status = entry.session.status()
        if entry.error or status["state"] == "failed":
            raise RuntimeError(entry.error or status["error"])
        frame = entry.session.frame_info()
        if not frame or not frame["complete"] or status["generation"] <= self.generation:
            return False
        if frame["generation"] != status["generation"] or self.panel._live_generation != frame["generation"]:
            return False
        raw = entry.session.frame()
        pixels = np.frombuffer(raw["planes"]["combined"]["pixels"], np.float32).reshape(144, 192, 4)
        actual = pixels[103:106, 47:50, :3].mean(axis=(0, 1))
        assert np.max(np.abs(actual - np.asarray(expected))) < 0.025, (actual.tolist(), expected)
        assert not self.panel.canvas.image.isNull()
        if hasattr(self, "conversions"):
            if reuse:
                assert status["geometry_conversions"] == self.conversions, status
            assert status["renderer_pid"] == self.pid, status
        assert len(registry.entries()) == 1 and entry.views == 1
        self.report["events"].append(dict(stage=self.stage, rgb=actual.tolist(), status=status))
        self.generation = status["generation"]
        return True

    def tick(self):
        try:
            if time.monotonic() - self.started > 90:
                raise TimeoutError(self.stage + ": " + self.panel.info.text())
            if time.monotonic() < self.next_at:
                return
            entry = self.panel.entry
            tint = (0.8, 0.6, 1)
            if self.stage == "initial":
                if not self.ready(entry, [v * v * math.exp(-2) for v in tint]):
                    return
                for observer in (entry.observer, self.panel._observer):
                    assert any(node == self.medium for node, events in observer.nodes), [
                        node.path() for node, events in observer.nodes
                    ]
                self.conversions = entry.session.status()["geometry_conversions"]
                self.pid = entry.session.status()["renderer_pid"]
                self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(700, 650))
                self.settings.setCurrentNode(self.medium)
                self.settings.setPin(True)
                window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
                window.show()
                window.raise_()
                window.activateWindow()
                self.settings.setIsCurrentTab()
                self.settings.moveFocusTo("density")
                self.stage = "native-focus"
                self.next_at = time.monotonic() + 0.5
            elif self.stage == "native-focus":
                focus = QtWidgets.QApplication.focusWidget() or hou.qt.floatingPanelWindow(
                    self.settings.floatingPanel()
                )
                assert focus
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, "1")
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.stage = "density-edit"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "density-edit":
                assert abs(self.medium.evalParm("density") - 1) < 1e-5, self.medium.evalParm("density")
                if not self.ready(entry, [v * v * math.exp(-1) for v in tint]):
                    return
                self.panel.grab().save(str(self.path.with_suffix(".png")))
                QtTest.QTest.mouseClick(self.panel.snapshot_button, QtCore.Qt.LeftButton)
                assert len(self.panel._snapshots) == 1
                self.output.setInput(self.output.inputIndex("volume"), None)
                self.stage = "without-medium"
            elif self.stage == "without-medium":
                if not self.ready(entry, [v * v for v in tint]):
                    return
                self.output.setNamedInput("volume", self.medium, "volume")
                self.stage = "restored-medium"
            elif self.stage == "restored-medium":
                if not self.ready(entry, [v * v * math.exp(-1) for v in tint]):
                    return
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                replacement = create_material(name="new_assigned_medium", shader="transparent_bsdf")
                self.new_medium = replacement.createNode("cycles::absorption_volume::1", "medium")
                self.new_medium.parmTuple("color").set((0, 0, 0))
                self.new_medium.parm("density").set(0.5)
                replacement.node("surface_output").setNamedInput("volume", self.new_medium, "volume")
                hou.node("/obj/closed_medium").parm("shop_materialpath").set(replacement.path())
                self.stage = "new-assignment"
            elif self.stage == "new-assignment":
                if not self.ready(entry, [math.exp(-0.5)] * 3, reuse=False):
                    return
                for observer in (entry.observer, self.panel._observer):
                    assert any(node == self.new_medium for node, events in observer.nodes)
                self.conversions = entry.session.status()["geometry_conversions"]
                self.new_medium.parm("density").set(1.5)
                self.stage = "new-material-edit"
            elif self.stage == "new-material-edit":
                if not self.ready(entry, [math.exp(-1.5)] * 3):
                    return
                self.retired = entry.session
                self.panel.dispose()
                self.stage = "retirement"
            elif self.stage == "retirement":
                status = self.retired.status()
                if status["state"] != "closed":
                    return
                assert not status["backend_alive"] and status["device_allocation_bytes"] == 0
                self.finish(None)
                return
            self.path.write_text(json.dumps(self.report, indent=2))
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error):
        hou.ui.removeEventLoopCallback(self.tick)
        self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
