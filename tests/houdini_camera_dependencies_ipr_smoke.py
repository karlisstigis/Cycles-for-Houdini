# SPDX-License-Identifier: Apache-2.0
"""Independent position/radiance checks of camera-driven SOP and shader edits."""

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry
from gui_shutdown import retire_and_exit


class CameraDependencies:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.began = time.monotonic()
        self.busy = self.finishing = False
        self.stage = "initial"
        self.report = dict(result="running", checks=[], render_viewers=["cycles"])
        self.obj = hou.node("/obj").createNode("geo", "dependency_geometry", run_init_scripts=False)
        self.shape = self.obj.createNode("box")
        self.shape.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "dependency_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=3))
        pass_fixture.parm(self.shape, "sizez").setExpression('0.4+abs(ch("/obj/dependency_camera/tx"))')
        self.material = ch.create_material(name="dependency_emission", shader="emission")
        self.shader = self.material.node("surface_shader")
        self.shader.parmTuple("color").set((0.25, 0.125, 0.0625))
        pass_fixture.parm(self.shader, "strength").set(1)
        self.rop = hou.node("/out").createNode("cycles_ipr", "camera_dependency_rop")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        pass_fixture.set_parameters(
            self.rop,
            dict(
                soppath=self.shape.path(),
                camera=self.camera.path(),
                materialpath=self.material.path(),
                resx=96,
                resy=72,
                samples=8,
                pass_position=1,
                transparent_background=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.rop.parmTuple("lightstrength").set((0, 0, 0))
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.scene.setPwd(hou.node("/obj"))
        self.scene.curViewport().setCamera(self.camera)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.panel.set_rop(self.rop)
        self.panel.window().resize(800, 600)
        self.timer = QtCore.QTimer(self.panel)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        self.before = 0

    def center(self, entry, name):
        plane = entry.session.frame()["planes"][name]
        return np.frombuffer(plane["pixels"], np.float32).reshape(72, 96, plane["channels"])[36, 48]

    def tick(self):
        if self.busy or self.finishing:
            return
        self.busy = True
        try:
            if time.monotonic() - self.began > 80:
                raise TimeoutError("Camera dependency update stalled")
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            if (
                status["state"] != "complete"
                or status["generation"] <= self.before
                or self.panel.canvas.generation != status["generation"]
                or self.panel._settings_dirty
            ):
                return
            self.report.update(stage=self.stage, status=status)
            if self.stage == "initial":
                assert abs(self.center(entry, "position")[2] - 0.2) < 1e-5
                self.conversions = status["geometry_conversions"]
                self.before = status["generation"]
                pass_fixture.parm(self.camera, "tx").set(0.3)
                self.stage = "camera-driven-shape"
            elif self.stage == "camera-driven-shape":
                assert abs(self.center(entry, "position")[2] - 0.35) < 1e-5
                assert status["geometry_conversions"] == self.conversions + 1
                self.report["checks"].append(
                    "Camera expression changes rendered SOP shape, verified independently by Position"
                )
                pass_fixture.parm(self.shape, "sizez").deleteAllKeyframes()
                pass_fixture.parm(self.shape, "sizez").set(0.7)
                self.before = status["generation"]
                self.stage = "remove-shape-reference"
            elif self.stage == "remove-shape-reference":
                self.conversions = status["geometry_conversions"]
                self.cooks = self.shape.cookCount()
                self.before = status["generation"]
                pass_fixture.parm(self.camera, "tx").set(0.1)
                self.stage = "ordinary-camera"
            elif self.stage == "ordinary-camera":
                assert abs(self.center(entry, "position")[2] - 0.35) < 1e-5
                assert status["geometry_conversions"] == self.conversions and self.shape.cookCount() == self.cooks
                self.report["checks"].append("An ordinary camera move performs no SOP cook or geometry conversion")
                self.before = status["generation"]
                pass_fixture.parm(self.shader, "strength").setExpression('1+ch("/obj/dependency_camera/ty")')
                self.stage = "add-shader-reference"
            elif self.stage == "add-shader-reference":
                self.conversions = status["geometry_conversions"]
                self.before = status["generation"]
                pass_fixture.parm(self.camera, "ty").set(0.2)
                self.stage = "camera-driven-shader"
            elif self.stage == "camera-driven-shader":
                assert np.allclose(self.center(entry, "combined"), (0.3, 0.15, 0.075, 1), atol=1e-5, rtol=0)
                assert status["geometry_conversions"] == self.conversions
                self.report["checks"].append("Camera expression changes shader radiance without converting geometry")
                self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.finish(0)
                return
            self.save()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.finish(1)
        finally:
            self.busy = False

    def finish(self, code):
        if self.finishing:
            return
        self.finishing = True
        self.timer.stop()
        entry = self.panel.entry
        retire_and_exit(
            self.path,
            self.report,
            [entry.session] if entry else [],
            code,
            (self.panel.dispose, registry.close_all, self.tab.close, self.scene.close),
        )

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")


workflow = CameraDependencies()
