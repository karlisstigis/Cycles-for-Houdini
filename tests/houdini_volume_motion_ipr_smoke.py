# SPDX-License-Identifier: Apache-2.0
"""Live velocity-volume IPR, shared view ownership and failed-field recovery."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
from array import array
import os
from pathlib import Path
import time
import traceback

import hou
from cycles_houdini import registry
from ipr_update_checks import failed_update


class VolumeMotionLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage2/motion/volume/native-volume-velocity.hiplc"), suppress_save_prompt=True
        )
        self.rop = hou.node("/out/volume_velocity_cycles")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.source = hou.node(self.rop.evalParm("soppath"))
        self.missing = self.source.parent().node("missing_velocity")
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

    def pixel(self, world_x):
        column = round((world_x / 4 + 0.5) * 96)
        try:
            return (
                sum(self.viewer.pixel("C", x, y)[0] for x in range(column - 2, column + 2) for y in range(30, 34)) / 16
            )
        except hou.OperationFailed:
            return None

    def image(self, entry, expected, world_x, newer=True):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if newer and status["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != status["generation"]:
            return False
        value = self.pixel(world_x)
        return value is not None and abs(value - expected) < 0.06

    def raw_pixel(self, entry, world_x):
        if not entry:
            return None
        frame = entry.session.frame()
        if not frame:
            return None
        values = array("f")
        values.frombytes(frame["planes"]["combined"]["pixels"])
        column = round((world_x / 4 + 0.5) * frame["width"])
        middle = frame["height"] // 2
        return (
            sum(
                values[(y * frame["width"] + x) * 4]
                for x in range(column - 2, column + 2)
                for y in range(middle - 2, middle + 2)
            )
            / 16
        )

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                soho=entry.last_soho.copy(),
                centre_red=self.pixel(1),
                trail_red=self.pixel(1.75),
            )
        )
        self.save()

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
                trail_red=self.pixel(1.75),
                raw_trail_red=self.raw_pixel(entry, 1.75),
                rop_errors=self.rop.errors(),
                events=list(registry.debug_events),
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Volume velocity IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, 0.722, 1.75, newer=False):
                    return
                assert entry.session.status()["volume_ray_marching"]
                assert entry.last_soho["host_pid"] == os.getpid()
                self.entry = entry
                self.conversions = entry.session.status()["geometry_conversions"]
                self.record("Native IPR unclipped velocity trail", entry)
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                self.advance(1, entry)
                self.camera.parm("tz").set(6)
            elif self.stage == 1:
                if not self.image(entry, 0.722, 1.75):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Camera edit retained motion grids", entry)
                self.advance(2, entry)
                self.camera.parm("shutter").set(0.25)
            elif self.stage == 2:
                if not self.image(entry, 0.34, 0.5):
                    return
                self.record("Camera shutter changes volume advection", entry)
                self.advance(3, entry)
                self.rop.parm("allowmotionblur").set(0)
            elif self.stage == 3:
                if not self.image(entry, 0.325, 0):
                    return
                assert not entry.session.status()["volume_ray_marching"]
                self.record("Blur off restores static volume integrator", entry)
                self.advance(4, entry)
                self.camera.parm("shutter").set(0.5)
                self.rop.parm("allowmotionblur").set(1)
            elif self.stage == 4:
                if not self.image(entry, 0.722, 1.75):
                    return
                self.record("Blur on restores the full trail", entry)
                self.advance(5, entry)
                self.rop.parm("soppath").set(self.missing.path())
            elif self.stage == 5:
                if not self.image(entry, 0.325, 0):
                    return
                assert not entry.session.status()["volume_ray_marching"]
                assert entry.session.diagnostics()["session_cancel_calls"] == 0
                self.record("Missing velocity renders static density", entry)
                self.advance(6, entry)
                self.rop.parm("soppath").set(self.source.path())
            elif self.stage == 6:
                if not self.image(entry, 0.722, 1.75):
                    return
                assert self.panel.entry is entry and len(registry.entries()) == 1
                self.record("Valid fields recover both views", entry)
                self.advance(7, entry)
                self.rop.parm("volume_step_size").set(0.03125)
            elif self.stage == 7:
                if not self.image(entry, 0.722, 1.75):
                    return
                assert entry.session.status()["geometry_resources"] == 1
                self.record("Sampling step change updates one shared volume variant", entry)
                self.advance(8, entry)
                self.viewer.killRender()
            elif self.stage == 8:
                if entry.session.status()["state"] != "cancelled":
                    return
                self.record("Native cancellation with panel retained", entry)
                self.panel.dispose()
                self.panel_tab.close()
                self.stage = 9
            elif self.stage == 9:
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


test = VolumeMotionLifecycle()
