# SPDX-License-Identifier: Apache-2.0
"""Live native point/curve assignment edits, missing builders and shared views."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import cycles_houdini as ch
from cycles_houdini import registry
from ipr_update_checks import recovered_update


class StrandAssignmentLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/materials/strands/native-strand-materials.hiplc"), suppress_save_prompt=True
        )
        hou.setFrame(1)
        self.rop = hou.node("/out/native_strand_assignment_cycles")
        self.obj = hou.node("/obj/strand_assignment_receiver")
        self.source = self.obj.node("sphere_points")
        self.hair = self.obj.node("linear_curves")
        self.assignment = self.obj.node("point_material_sop")
        self.red = hou.node("/mat/strand_red")
        self.blue = hou.node("/mat/strand_blue")
        self.green = hou.node("/mat/strand_green")
        self.colors = dict(red=(0.8, 0.1, 0.05), blue=(0.05, 0.1, 0.8), green=(0.05, 0.7, 0.1), missing=(0.2, 0.3, 0.4))
        self.paths = [self.red.path(), self.blue.path(), "", ""]
        self.missing_path = "/mat/live_strand_missing"
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0.0
        self.stage = 0
        self.generation = 0
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

    def image(self, entry, names, gains=None, newer=True):
        if not entry or entry.error:
            return False
        state = entry.session.status()
        frame = entry.session.frame_info()
        if newer and state["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != state["generation"]:
            return False
        try:
            pixels = [self.viewer.pixel("C", x, 48)[:3] for x in (19, 38, 57, 76)]
        except hou.OperationFailed:
            return False
        gains = gains or (1, 1, 1, 1)
        return all(
            abs(a - b * gain) < 0.001
            for pixel, name, gain in zip(pixels, names, gains)
            for a, b in zip(pixel, self.colors[name])
        )

    def record(self, name, entry, reuse=False):
        if reuse:
            assert entry.session.status()["geometry_conversions"] == self.conversions
            assert entry.session.status()["material_binding_scans"] == self.scans
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                materials=entry.session.native_material_references(),
                soho=entry.last_soho.copy(),
                helper_pid=entry.helper_pid,
            )
        )
        self.save()

    def advance(self, stage, entry):
        state = entry.session.status()
        self.generation = state["generation"]
        self.conversions = state["geometry_conversions"]
        self.scans = state["material_binding_scans"]
        self.stage = stage

    def failed(self, entry):
        return recovered_update(entry, self.generation, "Missing Cycles material")

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
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Native point/curve material IPR stalled")
            if self.stage == 0:
                if (
                    not entry
                    or not entry.helper_pid
                    or not self.image(entry, ("red", "blue", "green", "green"), newer=False)
                ):
                    return
                assert entry.last_soho["host_pid"] == os.getpid() and entry.session.status()["points"] == 4
                self.record("Independent per-point radiance in native IPR", entry)
                before = entry.session.status()
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
                    assert active[name] == before[name]
                entry.session.cancel()
                joined = entry.session.status()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                assert entry.session.status()["device_allocation_bytes"] == joined["device_allocation_bytes"]
                self.report["panel_memory"] = dict(active_before=before, active_after=active, joined=joined)
                self.record("Native/panel ownership and quiescent allocations are shared", entry)
                self.advance(1, entry)
                self.blue.node("surface_shader").parm("strength").set(0.5)
            elif self.stage == 1:
                if not self.image(entry, ("red", "blue", "green", "green"), gains=(1, 0.5, 1, 1)):
                    return
                self.record("Referenced point shader edit retains geometry and binding scan", entry, reuse=True)
                self.advance(2, entry)
                self.blue.node("surface_shader").parm("strength").set(1.0)
            elif self.stage == 2:
                if not self.image(entry, ("red", "blue", "green", "green")):
                    return
                self.record("Restored shader updates automatically", entry, reuse=True)
                self.advance(3, entry)
                self.rop.parm("soppath").set(self.assignment.path())
            elif self.stage == 3:
                if not self.image(entry, ("red", "blue", "blue", "green")):
                    return
                self.record("Material SOP point group updates the shared render", entry)
                self.advance(4, entry)
                with hou.undos.group("Native point group material"):
                    self.assignment.parm("shop_materialpath1").set(self.green.path())
            elif self.stage == 4:
                if not self.image(entry, ("red", "blue", "green", "green")):
                    return
                self.record("Point material reassignment updates the selected sphere", entry)
                self.advance(5, entry)
                hou.undos.performUndo()
            elif self.stage == 5:
                if not self.image(entry, ("red", "blue", "blue", "green")):
                    return
                self.record("Point binding undo restores the prior shader", entry)
                self.advance(6, entry)
                hou.undos.performRedo()
            elif self.stage == 6:
                if not self.image(entry, ("red", "blue", "green", "green")):
                    return
                self.record("Point binding redo restores the group assignment", entry)
                self.advance(7, entry)
                self.rop.parm("soppath").set(self.hair.path())
            elif self.stage == 7:
                if not self.image(entry, ("red", "blue", "green", "green")):
                    return
                assert entry.session.status()["curves"] == 4 and entry.session.status()["points"] == 0
                assert entry.session.status()["geometry_resources"] == 1 and len(registry.entries()) == 1
                self.record("Sphere-to-hair switch retains one shared renderer", entry)
                self.advance(8, entry)
                self.hair.parm("bindings").set(json.dumps([self.green.path(), self.blue.path(), self.red.path(), ""]))
            elif self.stage == 8:
                if not self.image(entry, ("green", "blue", "red", "green")):
                    return
                self.record("Curve primitive palettes update while their key positions stay fixed", entry)
                self.advance(9, entry)
                self.hair.parm("bindings").set(json.dumps([self.missing_path, self.blue.path(), self.red.path(), ""]))
            elif self.stage == 9:
                if not self.failed(entry):
                    return
                self.record("Missing curve material renders a diagnostic surface without dropping the curves", entry)
                self.stage = 10
                self.replacement = ch.create_material(name=self.missing_path.rsplit("/", 1)[1], shader="emission")
                self.replacement.node("surface_shader").parmTuple("color").set(self.colors["missing"])
                self.replacement.node("surface_shader").parm("strength").set(1.0)
            elif self.stage == 10:
                if not self.image(entry, ("missing", "blue", "red", "green")):
                    return
                self.record("Creating the missing builder recovers through parent-network observation", entry)
                self.advance(11, entry)
                self.replacement.destroy()
            elif self.stage == 11:
                if not self.failed(entry):
                    return
                self.record("Deleting an assigned curve builder reports its stale binding", entry)
                self.stage = 12
                self.hair.parm("bindings").set(json.dumps(self.paths))
            elif self.stage == 12:
                if not self.image(entry, ("red", "blue", "green", "green")):
                    return
                assert entry.session.status()["material_resources"] == 4
                self.record("Corrected curve bindings recover and retire the obsolete material", entry)
                self.advance(13, entry)
                self.panel.poll()
                preview = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "native-strand-material-panel.png"
                assert self.panel.grab().save(str(preview))
                self.report["panel_preview"] = str(preview)
                self.viewer.killRender()
                self.viewer.close()
            elif self.stage == 13:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Native viewer closes while panel retains assigned hair", entry)
                self.panel.render()
                self.stage = 14
            elif self.stage == 14:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                self.record("Panel continues the same assigned-hair session", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
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
        self.report["result"] = "passed" if code == 0 else "failed"
        self.save()
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


test = StrandAssignmentLifecycle()
