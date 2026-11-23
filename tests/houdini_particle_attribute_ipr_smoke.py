# SPDX-License-Identifier: Apache-2.0
"""Native IPR treats simulation attributes as ordinary data, without duplicate viewers."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json, os, time, traceback
from pathlib import Path
import hou
import cycles_houdini as ch
from cycles_houdini import registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
exec((root / "tests/particle_attribute_fixture.py").read_text())


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        hou.setFrame(1)
        self.obj, self.source = particle_attribute_fixture()
        material = ch.create_material(name="particle_surface", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        material.node("surface_shader").setParms(dict(strength=10.0))
        material.node("surface_shader").parmTuple("color").set((0.2, 0.7, 0.1))
        self.obj.parm("shop_materialpath").set(material.path())
        cam = hou.node("/obj").createNode("cam")
        cam.setParms(dict(tz=6, projection=1, orthowidth=6))
        self.rop = hou.node("/out").createNode("cycles_ipr", "particle_attributes")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=cam.path(),
                resx=192,
                resy=96,
                samples=64,
                ipr_samples=64,
                geometry_motion="velocity",
                allowmotionblur=1,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.key = f"rop:{self.rop.sessionId()}"
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self.started = time.monotonic()
        self.stage = 0
        self.checks = -1
        self.report = dict(result="running", events=[])
        hou.ui.addEventLoopCallback(self.tick)
        self.viewer.startRender()

    def tick(self):
        try:
            if time.monotonic() - self.started > 100:
                raise TimeoutError(str(self.stage))
            entry = registry.get(self.key)
            if not entry:
                return
            if entry.error:
                raise RuntimeError(entry.error)
            status = entry.session.status()
            info = entry.session.frame_info()
            if (
                status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
            ):
                return
            if (status["component_checks"] + status["cache_hits"]) <= self.checks:
                return
            if self.stage == 0:
                self.conversions = status["geometry_conversions"]
            assert (
                status["geometry_conversions"] == self.conversions
                and status["points"] == 6
                and not status["source_diagnostics"]
            ), status
            try:
                pixel = self.viewer.pixel("C", 48, 29)
            except hou.OperationFailed:
                return
            assert pixel[1] > 0.1, pixel
            assert len(registry.entries()) == 1 and entry.views == 0 and entry.channel
            self.checks = status["component_checks"] + status["cache_hits"]
            self.report["events"].append(dict(mode=self.stage, status=status, pixel=list(pixel)))
            self.path.write_text(json.dumps(self.report, indent=2))
            if self.stage == 6:
                self.finish(None)
                return
            self.stage += 1
            self.source.parm("mode").set(self.stage)
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error):
        hou.ui.removeEventLoopCallback(self.tick)
        self.viewer.killRender()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
