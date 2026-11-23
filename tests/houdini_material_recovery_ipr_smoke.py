# SPDX-License-Identifier: Apache-2.0
"""Stock asset recovery through the SOHO pipe and one native Houdini Render View."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import registry


class Workflow:
    def __init__(self):
        self.report = dict(result="running", events=[], device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.started = time.monotonic()
        self.stage = 0
        self.generation = 0
        obj = hou.node("/obj").createNode("geo", "native_recovery_pig", run_init_scripts=False)
        pig = obj.createNode("testgeometry_pighead")
        self.assignment = obj.createNode("attribwrangle")
        self.assignment.setInput(0, pig)
        self.assignment.parm("class").set(1)
        self.assignment.addSpareParmTuple(hou.StringParmTemplate("material", "Material", 1))
        self.assignment.parm("snippet").set('string path=chs("material"); if(path!="") s@shop_materialpath=path;')
        self.assignment.setRenderFlag(True)
        self.assignment.setDisplayFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "native_recovery_camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.material = self.make_material()
        self.rop = hou.node("/out").createNode("cycles_ipr", "native_recovery")
        set_rop_devices(self.rop, self.report["device"])
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=obj.path(),
                camera=self.camera.path(),
                resx=192,
                resy=144,
                samples=8,
                ipr_samples=8,
                allowmotionblur=0,
                use_denoise=0,
                ipr_use_denoise=0,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.key = f"rop:{self.rop.sessionId()}"
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        hou.ui.addEventLoopCallback(self.tick)
        self.viewer.startRender()

    def make_material(self):
        material = ch.create_material(name="native_recovery_emission", shader="emission")
        material.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
        material.node("surface_shader").parm("strength").set(1)
        return material

    def tick(self):
        try:
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Native recovery stage " + str(self.stage))
            entry = registry.get(self.key)
            if not entry:
                return
            status = entry.session.status()
            if entry.error or status["error"]:
                raise RuntimeError(entry.error or status["error"])
            frame = entry.session.frame()
            if not frame or not frame["complete"] or frame["generation"] <= self.generation:
                return
            expected_warning = self.stage in (0, 2)
            if bool(status["source_diagnostics"]) != expected_warning:
                return
            try:
                displayed = self.viewer.pixel("C", 96, 72)[:3]
            except hou.OperationFailed:
                return
            expected = (1, 0, 1) if expected_warning else (0.1, 0.7, 0.2)
            if not np.allclose(displayed, expected, atol=1e-4):
                return
            assert len(registry.entries()) == 1 and entry.views == 0
            assert entry.helper_pid and not self.rop.errors()
            self.report["events"].append(
                dict(stage=self.stage, status=status, native_pixel=list(displayed), helper_pid=entry.helper_pid)
            )
            self.generation = frame["generation"]
            if self.stage == 0:
                self.assignment.parm("material").set(self.material.path())
            elif self.stage == 1:
                self.material.destroy()
            elif self.stage == 2:
                self.material = self.make_material()
            elif self.stage == 3:
                self.camera.parm("tx").set(0.1)
            else:
                self.finish(None)
                return
            self.stage += 1
            self.path.write_text(json.dumps(self.report, indent=2))
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error):
        hou.ui.removeEventLoopCallback(self.tick)
        self.viewer.killRender()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
