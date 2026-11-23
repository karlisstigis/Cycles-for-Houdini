# SPDX-License-Identifier: Apache-2.0
"""Actual native material IPR edits, undo/redo, rewiring and shared-view recovery."""

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
from ipr_update_checks import recovered_update


class MaterialLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/materials/vops/native-material-vops.hiplc"), suppress_save_prompt=True
        )
        hou.setFrame(1)
        self.rop = hou.node("/out/native_material_cycles")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.builder = hou.node("/mat/native_surface")
        self.color = self.builder.node("base_color")
        self.emission = next(node for node in self.builder.children() if node.type().name() == "cycles::emission::1")
        self.output = self.builder.node("surface_output")
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

    def image(self, entry, expected, newer=True):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if newer and status["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != status["generation"]:
            return False
        try:
            pixel = self.viewer.pixel("C", 32, 32)[:3]
        except hou.OperationFailed:
            return False
        return all(abs(value - target) < 0.001 for value, target in zip(pixel, expected))

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                soho=entry.last_soho.copy(),
                helper_pid=entry.helper_pid,
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
                error=entry.error if entry else "",
                status=entry.session.status() if entry else None,
                rop_errors=self.rop.errors(),
                events=list(registry.debug_events),
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Native material IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, (0.4, 0.8, 1.6), newer=False):
                    return
                assert entry.last_soho["host_pid"] == os.getpid()
                self.entry = entry
                self.conversions = entry.session.status()["geometry_conversions"]
                self.record("Independently specified HDR radiance in native IPR", entry)
                status = entry.session.status()
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                active = entry.session.status()
                for name in (
                    "generation",
                    "geometry_resources",
                    "material_resources",
                    "engine_shader_resources",
                    "owned_geometry_bytes",
                ):
                    assert active[name] == status[name], (name, status, active)
                entry.session.cancel()
                quiescent = entry.session.status()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                assert entry.session.status()["device_allocation_bytes"] == quiescent["device_allocation_bytes"]
                self.report["panel_memory"] = dict(
                    active_before=status,
                    active_after=active,
                    quiescent_before=quiescent,
                    quiescent_after=entry.session.status(),
                )
                self.record("Panel and native viewer share one material render", entry)
                self.advance(1, entry)
                self.color.parmTuple("value").set((0.3, 0.2, 0.1))
            elif self.stage == 1:
                if not self.image(entry, (0.6, 0.4, 0.2)):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Native VOP parameter edit without geometry conversion", entry)
                self.advance(2, entry)
                with hou.undos.group("Native material gain edit"):
                    self.builder.parm("gain").deleteAllKeyframes()
                    self.builder.parm("gain").set(3.0)
            elif self.stage == 2:
                if not self.image(entry, (0.9, 0.6, 0.3)):
                    return
                self.record("Promoted material control updates both viewers", entry)
                self.advance(3, entry)
                hou.undos.performUndo()
            elif self.stage == 3:
                if not self.image(entry, (0.6, 0.4, 0.2)):
                    return
                self.record("Native material undo", entry)
                self.advance(4, entry)
                hou.undos.performRedo()
            elif self.stage == 4:
                if not self.image(entry, (0.9, 0.6, 0.3)):
                    return
                self.record("Native material redo", entry)
                self.advance(5, entry)
                self.builder.parm("gain").setExpression("$F + 1", hou.exprLanguage.Hscript)
                hou.setFrame(3)
            elif self.stage == 5:
                if not self.image(entry, (1.2, 0.8, 0.4)):
                    return
                self.record("Animated graph evaluated after frame change", entry)
                self.advance(6, entry)
                self.color.setName("renamed_native_color")
            elif self.stage == 6:
                if not self.image(entry, (1.2, 0.8, 0.4)):
                    return
                self.record("Native material rename retains rendering state", entry)
                self.advance(7, entry)
                self.replacement = self.builder.createNode("cycles::emission::1", "replacement")
                self.replacement.parmTuple("color").set((0.1, 0.2, 0.7))
                self.replacement.parm("strength").set(2.0)
                self.output.setNamedInput("surface", self.replacement, "emission")
            elif self.stage == 7:
                if not self.image(entry, (0.2, 0.4, 1.4)):
                    return
                self.record("Created and rewired surface closure updates native IPR", entry)
                self.advance(8, entry)
                self.replacement.destroy()
            elif self.stage == 8:
                if not recovered_update(entry, self.generation, "Connect a surface closure"):
                    return
                self.record("Deleted closure reports a warning and renders a diagnostic surface", entry)
                self.stage = 9
                self.output.setNamedInput("surface", self.emission, "emission")
            elif self.stage == 9:
                if not self.image(entry, (1.2, 0.8, 0.4)):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Rewiring recovers the shared render", entry)
                self.advance(10, entry)
                self.rop.parm("materialpath").set("/mat/missing_native_material")
            elif self.stage == 10:
                if not recovered_update(entry, self.generation, "Missing Cycles material"):
                    return
                self.record("Missing material path renders a visible diagnostic surface", entry)
                self.stage = 11
                self.rop.parm("materialpath").set(self.rop.relativePathTo(self.builder))
            elif self.stage == 11:
                if not self.image(entry, (1.2, 0.8, 0.4)):
                    return
                self.record("Relative material binding recovers automatically", entry)
                self.panel.poll()
                image_path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "native-material-panel.png"
                assert self.panel.grab().save(str(image_path))
                self.report["panel_preview"] = str(image_path)
                self.viewer.killRender()
                self.viewer.close()
                self.stage = 12
            elif self.stage == 12:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Native viewer closes while panel retains session ownership", entry)
                self.panel.render()
                self.stage = 13
            elif self.stage == 13:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Dedicated panel continues the same authored material", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        self.report["result"] = "passed" if code == 0 else "failed"
        self.save()
        hou.ui.removeEventLoopCallback(self._tick)
        try:
            self.viewer.killRender()
            self.viewer.close()
        except (hou.ObjectWasDeleted, hou.OperationFailed):
            pass
        if hasattr(self, "panel"):
            self.panel.dispose()
            try:
                self.panel_tab.close()
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
        registry.close_all()
        self._exit_code = code
        self._exit_at = time.monotonic() + 0.5
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if time.monotonic() >= self._exit_at:
            hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(lambda: self.exit(self._exit_code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = MaterialLifecycle()
