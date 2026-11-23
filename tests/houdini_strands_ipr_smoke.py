# SPDX-License-Identifier: Apache-2.0
"""Live particles/hair, shared native IPR/panel, edits, failures and shutdown."""

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


class StrandsLifecycleTest:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(str(artifacts_root() / "stage2/particles/native-particles.hiplc"), suppress_save_prompt=True)
        self.rop = hou.node("/out/cycles_particles")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.particles = hou.node(self.rop.evalParm("soppath"))
        self.edit = self.particles.parent().createNode("attribwrangle", "interactive_particle_edit")
        self.edit.setInput(0, self.particles)
        self.edit.parm("snippet").set("@pscale *= 0.5; v@Cd = set(0.05, 0.7, 0.1);")
        self.hair = self.particles.parent().createNode("python", "interactive_hair")
        self.hair_code = """import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Prim, 'id', 10)
g.addAttrib(hou.attribType.Point, 'width', 0.2); g.addAttrib(hou.attribType.Point, 'Cd', (0.8, 0.1, 0.02))
curve = g.createPolygon(is_closed=False)
for y in (-0.6, 0.6):
    p = g.createPoint(); p.setPosition((-0.8, y, 0)); curve.addVertex(p)
"""
        self.hair.parm("python").set(self.hair_code)
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

    def current_pixel(self, entry, expected):
        if not self.current_image(entry):
            return False
        try:
            pixel = self.viewer.pixel("C", 92, 90)  # Camera X=.05 after stage 1.
        except hou.OperationFailed:
            return False
        return max(abs(a - b) for a, b in zip(pixel, expected)) < 0.025

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
            if time.monotonic() - self.started > 80:
                raise TimeoutError("Particle/hair native IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.current_pixel(entry, (0.8, 0.1, 0.02)):
                    return
                assert not entry.session.frame_info()["complete"], "No unfinished particle image"
                assert entry.session.status()["points"] == 2 and entry.session.status()["curves"] == 0
                assert entry.last_soho["host_pid"] == os.getpid() and entry.helper_pid != os.getpid()
                self.entry = entry
                self.conversions = entry.session.status()["geometry_conversions"]
                self.generation = entry.session.status()["generation"]
                self.record("progressive native particles", entry)
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                assert entry.session.status()["generation"] == self.generation
                self.camera.parm("tx").set(0.05)
                self.stage = 1
            elif self.stage == 1:
                if entry.session.status()["generation"] <= self.generation or not self.current_pixel(
                    entry, (0.8, 0.1, 0.02)
                ):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("camera edit retained point geometry", entry)
                self.generation = entry.session.status()["generation"]
                self.rop.parm("soppath").set(self.edit.path())
                self.stage = 2
            elif self.stage == 2:
                if entry.session.status()["generation"] <= self.generation or not self.current_pixel(
                    entry, (0.05, 0.7, 0.1)
                ):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions + 1
                assert entry.session.status()["points"] == 2
                self.record("particle radius and color edit", entry)
                self.generation = entry.session.status()["generation"]
                self.rop.parm("soppath").set(self.hair.path())
                self.stage = 3
            elif self.stage == 3:
                if entry.session.status()["generation"] <= self.generation or not self.current_pixel(
                    entry, (0.8, 0.1, 0.02)
                ):
                    return
                status = entry.session.status()
                assert entry is self.entry and self.panel.entry is entry and len(registry.entries()) == 1
                assert status["curves"] == 1 and status["points"] == 0
                assert status["geometry_resources"] == status["object_resources"] == 1, status
                self.record("point-to-hair replacement in same session", entry)
                self.generation = status["generation"]
                self.conversions = status["geometry_conversions"]
                self.rop.parm("emission_strength").set(0.5)
                self.stage = 4
            elif self.stage == 4:
                if entry.session.status()["generation"] <= self.generation or not self.current_pixel(
                    entry, (0.4, 0.05, 0.01)
                ):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("hair material edit retained geometry", entry)
                self.generation = entry.session.status()["generation"]
                self.accepted = entry.session.status()
                self.hair.parm("python").set(self.hair_code.replace("'width', 0.2", "'width', -0.2"))
                self.stage = 5
            elif self.stage == 5:
                status = entry.session.status()
                if not entry.error or not status.get("update_rejected"):
                    return
                assert "radius" in entry.error and status["state"] == "failed"
                assert status["generation"] > self.generation and status["committed_generation"] == self.generation
                assert entry.session.frame_info()["generation"] == self.generation
                for name in (
                    "renderer_pid",
                    "backend_epoch",
                    "geometry_conversions",
                    "owned_geometry_bytes",
                    "geometry_resources",
                    "object_resources",
                ):
                    assert status[name] == self.accepted[name], (name, status[name], self.accepted[name])
                assert status["backend_alive"] and status["backend_state"] in ("rendering", "complete")
                assert entry.session.diagnostics()["session_cancel_calls"] == 0
                self.record("invalid hair width rejected; accepted scene, worker and last image retained", entry)
                self.hair.parm("python").set(self.hair_code)
                self.stage = 6
            elif self.stage == 6:
                if (
                    entry.error
                    or entry.session.status()["generation"] <= self.generation
                    or not self.current_pixel(entry, (0.4, 0.05, 0.01))
                ):
                    return
                self.record("valid edit recovered shared viewers", entry)
                self.viewer.killRender()
                self.stage = 7
            elif self.stage == 7:
                if entry.session.status()["state"] != "cancelled":
                    return
                assert self.panel.entry is entry
                self.record("native cancellation with panel retained", entry)
                self.panel.dispose()
                self.panel_tab.close()
                self.stage = 8
            elif self.stage == 8:
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


test = StrandsLifecycleTest()
