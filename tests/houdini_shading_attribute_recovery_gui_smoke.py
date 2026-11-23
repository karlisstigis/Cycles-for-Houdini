# SPDX-License-Identifier: Apache-2.0
"""One Cycles Viewer; native parameter edits break and repair UV/Cd during IPR."""

from support.paths import artifacts_root

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        texture = self.path.with_suffix(".exr")
        writer = OIIO.ImageOutput.create(str(texture))
        assert writer.open(str(texture), OIIO.ImageSpec(2, 2, 3, OIIO.FLOAT))
        assert writer.write_image(np.broadcast_to(np.array((0.2, 0.6, 0.3), np.float32), (2, 2, 3)).copy())
        assert writer.close()
        self.material = ch.create_material(name="artist_attribute_material", shader="emission")
        self.surface = self.material.node("surface_shader")
        pass_fixture.parm(self.surface, "strength").set(1)
        self.image = self.material.createNode("cycles::image_texture::1")
        pass_fixture.parm(self.image, "filename").set(str(texture))
        pass_fixture.parm(self.image, "colorspace").set("data")
        self.color = self.material.createNode("cycles::attribute::1")
        pass_fixture.parm(self.color, "attribute").set("Cd")
        self.bw = self.material.createNode("cycles::rgb_to_bw::1")
        self.height = self.material.createNode("cycles::displacement::1")
        pass_fixture.set_parameters(self.height, dict(height=0.25, midlevel=0.0, scale=1.0, space="object"))
        self.material.node("surface_output").setNamedInput("displacement", self.height, "displacement")
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        self.objects = []
        for index, x in enumerate((-1, 1)):
            obj = hou.node("/obj").createNode("geo", "artist_attribute_" + str(index), run_init_scripts=False)
            pass_fixture.parm(obj, "tx").set(x)
            pass_fixture.parm(obj, "shop_materialpath").set(self.material.path())
            grid = obj.createNode("grid")
            pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
            grid.parmTuple("size").set((1.5, 1.2))
            reverse = obj.createNode("reverse")
            reverse.setInput(0, grid)
            switch = obj.createNode("switch", "attribute_test")
            for slot, code in enumerate(
                (
                    "v@uv=set(.5,.5,0); v@Cd=set(.2,.6,.3);",
                    's@uv="broken"; v@Cd=set(.2,.6,.3);',
                    'v@uv=set(.5,.5,0); s@Cd="broken";',
                )
            ):
                wrangle = obj.createNode("attribwrangle")
                wrangle.setInput(0, reverse)
                pass_fixture.parm(wrangle, "snippet").set(code)
                switch.setInput(slot, wrangle)
            switch.setRenderFlag(True)
            switch.setDisplayFlag(True)
            self.objects.append(obj)
            if index:
                self.switch = switch
        self.camera = hou.node("/obj").createNode("cam", "artist_attribute_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "artist_attribute_recovery")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=" ".join(o.path() for o in self.objects),
                camera=self.camera.path(),
                resx=128,
                resy=64,
                samples=4,
                ipr_samples=4,
                allowmotionblur=0,
                pass_position=1,
                pass_uv=0,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setCurrentNode(self.switch)
        self.settings.setPin(True)
        self.panel = None
        self.started = time.monotonic()
        self.generation = 0
        self.next_at = 0
        self.cases = [("uv", False), ("uv", True), ("color", False), ("color", True)]
        self.case = 0
        self.stage = "mount"
        self.configure()
        example = artifacts_root() / "examples/attribute-recovery/attribute-recovery.hiplc"
        example.parent.mkdir(parents=True, exist_ok=True)
        for obj in self.objects:
            obj.layoutChildren()
        self.material.layoutChildren()
        hou.hipFile.save(str(example))
        self.report = dict(result="running", events=[])
        hou.ui.addEventLoopCallback(self.tick)

    def configure(self):
        mode, shared = self.cases[self.case]
        source = self.image if mode == "uv" else self.color
        self.surface.setNamedInput("color", source, "color")
        self.bw.setNamedInput("color", source, "color")
        if shared:
            self.height.setNamedInput("height", self.bw, "val")
        else:
            self.height.setInput(0, None)
            pass_fixture.parm(self.height, "height").set(0.25)

    def edit(self, value):
        self.pending_value = value
        self.settings.setIsCurrentTab()
        window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        window.show()
        window.raise_()
        window.activateWindow()
        self.settings.moveFocusTo("input")
        self.next_at = time.monotonic() + 0.25
        self.stage = "type"

    def ready(self, broken):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        if entry.error or status["state"] == "failed":
            raise RuntimeError(entry.error or status["error"])
        info = entry.session.frame_info()
        if (
            not info
            or not info["complete"]
            or info["generation"] <= self.generation
            or self.panel._live_generation != info["generation"]
        ):
            return False
        frame = entry.session.frame()

        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(64, 128, p["channels"])

        rgb = plane("combined")
        position = plane("position")
        assert np.allclose(rgb[32, 32, :3], (0.2, 0.6, 0.3), atol=1e-5), rgb[32, 32]
        assert np.allclose(rgb[32, 96, :3], (1, 0, 1) if broken else (0.2, 0.6, 0.3), atol=1e-5), rgb[32, 96]
        assert bool(status["source_diagnostics"]) == broken, status
        shared = self.cases[self.case][1]
        if shared:
            assert position[32, 32, 2] > 0.15
            assert abs(position[32, 96, 2] - (0 if broken else position[32, 32, 2])) < 1e-5
        else:
            assert np.allclose(position[32, [32, 96], 2], 0.25, atol=1e-5)
        assert len(registry.entries()) == 1 and entry.views == 1
        assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        self.report["events"].append(
            dict(case=self.cases[self.case], broken=broken, height=position[32, [32, 96], 2].tolist(), status=status)
        )
        self.generation = info["generation"]
        return True

    def tick(self):
        try:
            if time.monotonic() - self.started > 120:
                raise TimeoutError(self.stage + ": " + (self.panel.info.text() if self.panel else "No panel"))
            if time.monotonic() < self.next_at:
                return
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "healthy"
            elif self.stage == "healthy":
                if not self.ready(False):
                    return
                self.edit(1 if self.cases[self.case][0] == "uv" else 2)
            elif self.stage == "type":
                focus = QtWidgets.QApplication.focusWidget() or hou.qt.floatingPanelWindow(
                    self.settings.floatingPanel()
                )
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, str(self.pending_value))
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.stage = "broken" if self.pending_value else "repaired"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "broken":
                assert pass_fixture.evaluate(self.switch, "input") == self.pending_value
                if not self.ready(True):
                    return
                self.panel.grab().save(str(self.path.with_name(self.path.stem + "-" + str(self.case) + ".png")))
                self.edit(0)
            elif self.stage == "repaired":
                assert pass_fixture.evaluate(self.switch, "input") == 0
                if not self.ready(False):
                    return
                self.case += 1
                if self.case == len(self.cases):
                    QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                    self.finish(None)
                    return
                self.configure()
                self.stage = "healthy"
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
