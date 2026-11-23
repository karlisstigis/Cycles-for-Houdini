# SPDX-License-Identifier: Apache-2.0
"""Artist sky workflow on the million-triangle SOP fixture, one render viewer."""

import os
from pathlib import Path
import runpy
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "128"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="sky_fixture")[
    "HeavyResponse"
]


class SkyWorkflow(HeavyResponse):
    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.rop.setParms(dict(lights="", world_source="color", world_sampling="auto"))
        self.events = []

    def changed(self, name):
        self.before = self.panel.entry.session.status()["generation"]
        self.stage = name
        self.report["phase"] = name
        self.next_at = time.monotonic() + 0.4

    def retained(self, entry):
        status = entry.session.status()
        assert status["geometry_conversions"] == self.conversions
        assert status["displacement_rebuilds"] == self.displacement_builds
        assert status["renderer_pid"] == self.renderer_pid
        assert len(registry.entries()) == 1 and entry.views == 1
        frame = entry.session.frame()
        image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
        assert np.isfinite(image).all() and np.ptp(image) > 0
        self.events.append(dict(name=self.stage, status=status, diagnostics=entry.session.diagnostics()))

    def picture(self, name):
        for label, window in (
            ("render", self.panel.window()),
            ("parameters", hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel())),
        ):
            path = self.path.with_suffix("." + name + "." + label + ".png")
            assert window.screen().grabWindow(int(window.winId())).save(str(path))
            self.events.append(dict(name=name, window=label, screenshot=str(path)))

    def close(self, entry, code=0):
        registry.Entry.apply = self.original_apply
        registry.session_from_configuration = self.original_factory
        self.timer.stop()
        hou.ui.removeEventLoopCallback(self.callback)
        self.panel.dispose()
        registry.close_all()
        closed = entry.session.status() if entry else {}
        if code == 0:
            assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
            self.report.update(result="passed", closed=closed, events=self.events)
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(code, suppress_save_prompt=True))

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        entry = registry.get(self.key)
        try:
            if time.monotonic() - self.began > 200:
                raise TimeoutError(self.stage + ": " + self.panel.info.text())
            if entry is None:
                return
            self.report["events"] = self.events
            self.report["current"] = dict(stage=self.stage, status=entry.session.status(), error=entry.error)
            self.save()
            if time.monotonic() < self.next_at:
                return
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                self.conversions = entry.session.status()["geometry_conversions"]
                self.displacement_builds = entry.session.status()["displacement_rebuilds"]
                self.renderer_pid = entry.session.status()["renderer_pid"]
                self.settings_pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(800, 1000))
                self.scene.runShelfTool("cycles_create_sky_world")
                self.stage = "shelf-create"
                self.report["phase"] = self.stage
            elif self.stage == "shelf-create":
                nodes = hou.selectedNodes()
                if not nodes or nodes[0].type().name() != "cycles::world_material::1":
                    return
                self.world = nodes[0]
                self.sky = self.world.node("sky_texture")
                assert self.sky and self.world.node("background_shader").input(0) == self.sky
                self.settings_pane.setCurrentNode(self.sky)
                self.settings_pane.setPin(True)
                self.rop.setParms(dict(world_source="material", world_material=self.world.path()))
                self.panel.exposure.setValue(-6)
                self.changed("native-sky")
            elif self.stage == "native-sky":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("multiple-scattering")
                window = hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel())
                window.show()
                window.raise_()
                window.activateWindow()
                self.settings_pane.setIsCurrentTab()
                self.settings_pane.moveFocusTo("sun_elevation")
                self.stage = "native-focus"
                self.next_at = time.monotonic() + 0.5
            elif self.stage == "native-focus":
                # Houdini's native field can own keyboard focus without a Qt
                # child editor. Route keys through its public panel window in
                # that case; the following stage checks the actual node value.
                focus = QtWidgets.QApplication.focusWidget() or hou.qt.floatingPanelWindow(
                    self.settings_pane.floatingPanel()
                )
                assert focus, "Native parameter editor window is unavailable"
                self.report["native_editor_focus"] = dict(
                    class_name=focus.metaObject().className(), object_name=focus.objectName()
                )
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, "35")
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.changed("typed-sun-elevation")
            elif self.stage == "typed-sun-elevation":
                assert abs(self.sky.evalParm("sun_elevation") - 35) < 1e-5, self.sky.evalParm("sun_elevation")
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("native-degree-entry")
                self.sky.parm("sun_disc").set(0)
                self.sky.updateParmStates()
                assert self.sky.parm("sun_size").isHidden() and self.sky.parm("sun_intensity").isHidden()
                self.changed("sun-disabled")
            elif self.stage == "sun-disabled":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("sun-disabled")
                self.sky.parm("sky_type").set("preetham")
                self.sky.updateParmStates()
                assert not self.sky.parmTuple("sun_direction")[0].isHidden() and self.sky.parm("altitude").isHidden()
                self.panel.exposure.setValue(0)
                self.changed("preetham")
            elif self.stage == "preetham":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("preetham")
                self.sky.parm("sky_type").set("hosek_wilkie")
                self.sky.updateParmStates()
                assert not self.sky.parm("ground_albedo").isHidden()
                self.changed("hosek-wilkie")
            elif self.stage == "hosek-wilkie":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("hosek-wilkie")
                self.sky.setParms(dict(sky_type="single_scattering", sun_disc=1, sun_elevation=15))
                self.panel.exposure.setValue(-6)
                self.changed("single-scattering")
            elif self.stage == "single-scattering":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("single-scattering")
                before = entry.session.status()["generation"]
                self.panel.exposure.setValue(-5)
                assert entry.session.status()["generation"] == before
                self.camera.parm("tx").set(self.camera_x + 0.08)
                self.changed("camera-retains-sky")
            elif self.stage == "camera-retains-sky":
                if not self.ready(entry):
                    return
                self.retained(entry)
                assert "light_background_conditional_cdf" not in entry.session.diagnostics()["buffer_copy_requests"]
                self.close(entry)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc(), events=self.events)
            self.close(entry, 1)
        finally:
            self.tick_busy = False


workflow = SkyWorkflow()
