# SPDX-License-Identifier: Apache-2.0
"""Public HOM lifecycle test for a live in-memory, multi-field VDB render."""

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


class VolumeLifecycleTest:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        # Generated from the independent headless fixture; all grids are stashed.
        hou.hipFile.load(str(artifacts_root() / "stage2/pyro/native-pyro-fields.hiplc"), suppress_save_prompt=True)
        self.rop = next(node for node in hou.node("/out").children() if node.type().name() == "cycles_ipr")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.rop.parm("volume_emission_strength").set(2)
        self.rop.parm("blackbody_intensity").set(0)
        self.rop.parm("emission_field").set("emission")
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.stash = hou.node(self.rop.evalParm("soppath"))
        self.wrangle = self.stash.parent().createNode("volumewrangle", "interactive_emission_edit")
        self.wrangle.setInput(0, self.stash)
        self.wrangle.parm("snippet").set("@emission *= 0.5;")
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.stage = 0
        self.last_tick = 0
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
                helper_pid=entry.helper_pid,
                soho=entry.last_soho.copy(),
            )
        )
        self.save()

    def current_image(self, entry):
        frame = entry.session.frame_info()
        return frame and frame["generation"] == entry.session.status()["generation"]

    def tick(self):
        if time.monotonic() - self.last_tick < 0.1:
            return
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage,
                status=entry.session.status() if entry else None,
                rop_errors=self.rop.errors(),
                events=list(registry.debug_events),
            )
            self.save()
            if time.monotonic() - self.started > 70:
                raise TimeoutError("Volume native IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.current_image(entry):
                    return
                try:
                    pixel = self.viewer.pixel("C", 40, 24)
                except hou.OperationFailed:
                    return
                if pixel[0] < 0.3:
                    return
                assert not entry.session.frame_info()["complete"], "No unfinished volume image"
                assert entry.last_soho["host_pid"] == os.getpid() and entry.helper_pid != os.getpid()
                assert entry.session.status()["geometry_resources"] == entry.session.status()["object_resources"] == 1
                self.entry = entry
                self.conversions = entry.session.status()["geometry_conversions"]
                self.generation = entry.session.status()["generation"]
                self.record("progressive native volume image", entry)
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                assert entry.session.status()["generation"] == self.generation
                self.camera.parm("tx").set(0.05)
                self.stage = 1
            elif self.stage == 1:
                if entry.session.status()["generation"] <= self.generation or not self.current_image(entry):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("camera edit retained owned grids", entry)
                self.generation = entry.session.status()["generation"]
                self.rop.parm("volume_emission_strength").set(1)
                self.stage = 2
            elif self.stage == 2:
                if entry.session.status()["generation"] <= self.generation or not self.current_image(entry):
                    return
                if self.viewer.pixel("C", 40, 24)[0] > 0.4:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("material edit without grid conversion", entry)
                self.generation = entry.session.status()["generation"]
                self.rop.parm("soppath").set(self.wrangle.path())
                self.stage = 3
            elif self.stage == 3:
                if entry.session.status()["generation"] <= self.generation or not self.current_image(entry):
                    return
                if self.viewer.pixel("C", 40, 24)[0] > 0.2:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions + 1
                emission = next(
                    primitive
                    for primitive in self.wrangle.geometry().prims()
                    if primitive.attribValue("name") == "emission"
                )
                assert abs(emission.voxel((0, 0, 0)) - 0.125) < 1e-6
                self.record("grid edit replaced owned snapshot", entry)
                self.viewer.killRender()
                self.stage = 4
            elif self.stage == 4:
                if entry.session.status()["state"] != "cancelled":
                    return
                assert self.panel.entry is entry
                self.record("native cancellation with panel retained", entry)
                self.panel.dispose()
                self.panel_tab.close()
                self.stage = 5
            elif self.stage == 5:
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


test = VolumeLifecycleTest()
