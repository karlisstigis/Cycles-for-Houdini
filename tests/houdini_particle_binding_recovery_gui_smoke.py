# SPDX-License-Identifier: Apache-2.0
"""Shared particle bindings recover during actual viewer edits without dropping points."""

import json
import os
import time
import traceback
from pathlib import Path

import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_PARTICLE_BINDING_VIEWER") == "native"
        self.colors = ((0.8, 0.1, 0.05), (0.05, 0.7, 0.1), (0.05, 0.1, 0.8))
        scene = hou.node("/obj").createNode("subnet", "particle_binding_recovery")
        obj = scene.createNode("geo", "points", run_init_scripts=False)
        materials = []
        for name, color in zip(("first_owner", "point_owner", "second_owner"), self.colors):
            material = ch.create_material(name=name, shader="emission")
            material.node("surface_shader").parmTuple("color").set(color)
            material.node("surface_shader").parm("strength").set(1)
            materials.append(material)
        source = obj.createNode("python", "point_binding")
        source.addSpareParmTuple(hou.ToggleParmTemplate("repair", "Repair Shared Binding", default_value=True))
        source.addSpareParmTuple(
            hou.StringParmTemplate("material", "Point Material", 1, default_value=(materials[1].path(),))
        )
        source.parm("python").set("""import hou
node=hou.pwd();g=node.geometry();g.addAttrib(hou.attribType.Point,'shop_materialpath','')
g.addAttrib(hou.attribType.Point,'pscale',.24)
for index,x in enumerate((-1.,0.,1.,1.8)):
 point=g.createPoint();point.setPosition((x,0,0))
 if index==1 and node.evalParm('repair'):point.setAttribValue('shop_materialpath',node.evalParm('material'))
""")
        a = obj.createNode("add", "first_owner")
        a.setInput(0, source)
        a.setParms(dict(addparticlesystem=1, particlegroup="0 1"))
        b = obj.createNode("add", "second_owner")
        b.setInput(0, a)
        b.setParms(dict(addparticlesystem=1, particlegroup="1 2"))
        assigned = obj.createNode("material", "particle_materials")
        assigned.setInput(0, b)
        assigned.setParms(
            dict(
                num_materials=2,
                group1="0",
                group2="1",
                shop_materialpath1=materials[0].path(),
                shop_materialpath2=materials[2].path(),
            )
        )
        assigned.setDisplayFlag(True)
        assigned.setRenderFlag(True)
        camera = scene.createNode("cam", "camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.rop.setParms(
            dict(
                objectroot=scene.path(),
                vobject="*",
                lights="",
                camera=camera.path(),
                resx=160,
                resy=96,
                samples=8,
                ipr_samples=8,
                allowmotionblur=0,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                world_source="color",
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.control = source.parm("repair")
        self.key = "rop:" + str(self.rop.sessionId())
        self.began = time.monotonic()
        self.index = 0
        self.generation = 0
        self.worker = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.session = self.panel = None
        self.report = dict(result="running", viewer="native" if self.native else "cycles", events=[], scope=__doc__)
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
            self.tab.startRender()
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(900, 650))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and time.monotonic() < self.deadline:
                    return
                if status:
                    assert (
                        status["state"] == "closed"
                        and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0
                    ), status
                self.finished = True
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.path.write_text(json.dumps(self.report, indent=2))
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 180, (self.phase, self.index)
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.rop)
                    self.panel.auto.setChecked(True)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "render"
            entry = registry.get(self.key)
            if entry is None:
                return
            self.session = entry.session
            status = self.session.status()
            info = self.session.frame_info()
            assert status["state"] != "failed" and not entry.error, (status, entry.error)
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or info["generation"] <= self.generation
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            if not self.native and self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]:
                return
            frame = self.session.frame()
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])
            repaired = self.index % 2 == 0
            expected = (self.colors[0], self.colors[1] if repaired else self.colors[0], self.colors[2])
            for x, color in zip((40, 80, 120), expected):
                np.testing.assert_allclose(pixels[48, x, :3], color, atol=3e-4, rtol=0)
                if self.native:
                    try:
                        if not np.allclose(self.tab.pixel("C", x, 48)[:3], color, atol=3e-4, rtol=0):
                            return
                    except hou.OperationFailed:
                        return
            assert status["points"] == 3 and status["geometry_resources"] == 1 and not status["error"], status
            assert bool(status["source_diagnostics"]) != repaired, status
            if not repaired:
                assert any(
                    d["code"] == "material.conflicting_particle_binding" for d in status["source_diagnostics"]
                ), status
            if self.worker is None:
                self.worker = status["renderer_pid"]
            assert status["renderer_pid"] == self.worker and len(registry.entries()) == 1, status
            self.report["events"].append(dict(index=self.index, repaired=repaired, status=status))
            self.path.write_text(json.dumps(self.report, indent=2))
            self.generation = info["generation"]
            self.index += 1
            if self.index == 5:
                self.close()
                return
            self.control.set(self.index % 2 == 0)
        except BaseException:
            error = traceback.format_exc()
            if self.phase == "closing":
                self.finished = True
                self.report.update(result="failed", error=error)
                self.path.write_text(json.dumps(self.report, indent=2))
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(error)
        finally:
            self.busy = False

    def close(self, error=None):
        self.error = error
        self.phase = "closing"
        self.deadline = time.monotonic() + 20
        if self.native:
            self.tab.killRender()
        elif self.panel:
            self.panel.dispose()
        registry.close_all()


if __name__ == "__main__":
    workflow = Workflow()
