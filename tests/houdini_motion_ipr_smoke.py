# SPDX-License-Identifier: Apache-2.0
"""SOHO/native IPR shutter motion, shared panel and invalid-update recovery."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
from cycles_houdini import registry
from ipr_update_checks import failed_update


class MotionLifecycleTest:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage2/motion/native-velocity-motion.hiplc"), suppress_save_prompt=True
        )
        self.rop = hou.node("/out/cycles_native_motion")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.source = hou.node(self.rop.evalParm("soppath"))
        self.obj = self.source.parent()
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.code = self.source.evalParm("python")
        self.deformation = self.obj.createNode("xform", "interactive_deformation")
        self.deformation.setInput(0, self.source)
        self.deformation.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        self.bad_ids = self.obj.createNode("attribwrangle", "invalid_shutter_identity")
        self.bad_ids.setInput(0, self.deformation)
        self.bad_ids.parm("snippet").set("if (@Time > 0) i@id += 10000;")
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0
        self.stage = 0
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self._tick = self.tick
        hou.ui.addEventLoopCallback(self._tick)
        self.viewer.startRender()
        self.save()

    def save(self):
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                soho=entry.last_soho.copy(),
                measured_red=self.pixel(),
            )
        )
        self.save()

    def pixel(self):
        try:
            return sum(self.viewer.pixel("C", x, y)[0] for y in (44, 45) for x in (79, 80)) / 4
        except hou.OperationFailed:
            return None

    def image(self, entry, expected, newer=True):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if newer and status["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != status["generation"]:
            return False
        value = self.pixel()
        return value is not None and abs(value - expected) < 0.045

    def advance(self, stage, entry):
        self.generation = entry.session.status()["generation"]
        self.stage = stage

    def tick(self):
        if time.monotonic() - self.last_tick < 0.1:
            return
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage,
                status=entry.session.status() if entry else None,
                error=entry.error if entry else "",
                pixel=self.pixel(),
                rop_errors=self.rop.errors(),
                events=list(registry.debug_events),
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Native motion IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, 0.32, newer=False):
                    return
                self.entry = entry
                self.conversions = entry.session.status()["geometry_conversions"]
                assert entry.session.status()["motion_samples"] == 3
                assert entry.last_soho["host_pid"] == os.getpid()
                self.record("SOHO velocity half-frame shutter", entry)
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                self.advance(1, entry)
                self.camera.parm("tz").set(6)
            elif self.stage == 1:
                if not self.image(entry, 0.32):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Camera edit retains velocity geometry in shared views", entry)
                self.advance(2, entry)
                self.camera.parm("shutter").set(0.25)
            elif self.stage == 2:
                if not self.image(entry, 0.64):
                    return
                self.record("Camera shutter quarter-frame updates native velocity", entry)
                self.advance(3, entry)
                self.rop.parm("allowmotionblur").set(0)
            elif self.stage == 3:
                if not self.image(entry, 0.8):
                    return
                assert entry.session.status()["motion_samples"] == 0
                self.record("Blur disabled renders the current static frame", entry)
                self.advance(4, entry)
                self.camera.parm("shutter").set(0.5)
                self.rop.parm("allowmotionblur").set(1)
            elif self.stage == 4:
                if not self.image(entry, 0.32):
                    return
                self.record("Velocity blur re-enabled without a second session", entry)
                self.advance(5, entry)
                self.source.parm("python").set(
                    self.code.replace("g.addAttrib(hou.attribType.Point, 'v', (48.0, 0.0, 0.0))", "")
                )
            elif self.stage == 5:
                if not self.image(entry, 0.8):
                    return
                self.record("Missing velocity renders the static geometry", entry)
                self.advance(6, entry)
                self.source.parm("python").set(self.code)
            elif self.stage == 6:
                if not self.image(entry, 0.32):
                    return
                self.record("Valid velocity recovers both views", entry)
                self.advance(7, entry)
                self.rop.parm("geometry_motion").set("transform")
                self.obj.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
                self.camera.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
            elif self.stage == 7:
                if not self.image(entry, 0.8):
                    return
                assert self.panel.entry is self.entry and entry is self.entry and len(registry.entries()) == 1
                self.record("SOHO camera and OBJ sample the same shutter times", entry)
                self.advance(8, entry)
                self.obj.parm("tx").deleteAllKeyframes()
                self.obj.parm("tx").set(0)
                self.camera.parm("tx").deleteAllKeyframes()
                self.camera.parm("tx").set(0)
                self.rop.parm("geometry_motion").set("deformation")
                self.rop.parm("soppath").set(self.deformation.path())
            elif self.stage == 8:
                if not self.image(entry, 0.32):
                    return
                self.conversions = entry.session.status()["geometry_conversions"]
                self.record("SOHO samples actual subframe SOP deformation", entry)
                self.advance(9, entry)
                self.camera.parm("tz").set(7)
            elif self.stage == 9:
                if not self.image(entry, 0.32):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                assert entry.session.status()["motion_cooks"] == 0
                self.record("Live camera edit keeps deformation samples and avoids subframe cooks", entry)
                self.advance(10, entry)
                self.deformation.parm("tx").setExpression("$T * 24", hou.exprLanguage.Hscript)
            elif self.stage == 10:
                if not self.image(entry, 0.64):
                    return
                self.record("Centre-preserving deformation edit updates shutter samples", entry)
                self.advance(11, entry)
                self.rop.parm("soppath").set(self.bad_ids.path())
            elif self.stage == 11:
                if not failed_update(entry, self.generation, "identities change"):
                    return
                self.record("Invalid shutter identities report a failed edit", entry)
                self.rop.parm("soppath").set(self.deformation.path())
                self.stage = 12
            elif self.stage == 12:
                if not self.image(entry, 0.64):
                    return
                self.record("Valid deformation recovers both viewers", entry)
                self.advance(13, entry)
                self.viewer.killRender()
            elif self.stage == 13:
                if entry.session.status()["state"] != "cancelled":
                    return
                self.record("Native cancellation with the panel retained", entry)
                self.panel.dispose()
                self.panel_tab.close()
                self.stage = 14
            elif self.stage == 14:
                if entry:
                    return
                assert self.entry.session.status()["state"] == "closed"
                assert (
                    self.entry.session.status()["owned_geometry_bytes"]
                    == self.entry.session.status()["device_allocation_bytes"]
                    == 0
                )
                self.report["result"] = "passed"
                self.save()
                self.finish(0)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.finish(1)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self._tick)
        self.viewer.killRender()
        if hasattr(self, "panel"):
            self.panel.dispose()
        registry.close_all()
        self.exit_at = time.monotonic() + 0.5
        self.exit_code = code
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if time.monotonic() >= self.exit_at:
            hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(self.exit)

    def exit(self):
        try:
            hou.exit(self.exit_code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = MotionLifecycleTest()
