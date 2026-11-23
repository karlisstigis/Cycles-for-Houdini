# SPDX-License-Identifier: Apache-2.0
"""Actual native displaced-mesh IPR, undo, errors and shared panel ownership."""

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


class DisplacementLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage2/displacement/native-displacement.hiplc"), suppress_save_prompt=True
        )
        hou.setFrame(1)
        self.rop = hou.node("/out/native_displacement_cycles")
        self.builder = hou.node("/mat/native_displacement")
        self.offset = self.builder.node("offset")
        self.output = self.builder.node("surface_output")
        self.emission = self.builder.node("surface_shader")
        render_sop = hou.node(self.rop.evalParm("soppath"))
        # The +Z receiver now explicitly reverses Houdini's -Z XY Grid.
        # Resize its upstream Grid; the render SOP itself has no size parameter.
        assert render_sop.type().name() == "reverse"
        self.grid = render_sop.inputs()[0]
        assert self.grid.type().name() == "grid"
        self.camera = hou.node(self.rop.evalParm("camera"))
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(512)
        self.offset.parmTuple("vector").set((0, 0, 0.3))
        self.builder.parm("cycles_displacement_method").set("true")
        self.key = f"rop:{self.rop.sessionId()}"
        self.stage = 0
        self.generation = 0
        self.started = time.monotonic()
        self.last_tick = 0
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
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
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2))

    def image(self, entry, depth, newer=True):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if newer and status["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != status["generation"] or not frame["complete"]:
            return False
        try:
            measured = self.viewer.pixel("depth", 80, 45)[0]
            color = self.viewer.pixel("C", 80, 45)[:3]
        except hou.OperationFailed:
            return False
        self.report["measured_depth"] = measured
        return abs(measured - depth) < 0.001 and max(abs(a - b) for a, b in zip(color, (0.2, 0.4, 0.8))) < 0.003

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                depth=self.report.get("measured_depth"),
                status=entry.session.status(),
                soho=entry.last_soho.copy(),
            )
        )
        self.generation = entry.session.status()["generation"]
        self.save()

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
                raise TimeoutError("Displacement IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, 4.7, newer=False):
                    return
                self.entry = entry
                self.conversions = entry.session.status()["geometry_conversions"]
                self.record("Native IPR receives independently specified displaced depth", entry)
                before = entry.session.status()
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                for name in (
                    "generation",
                    "geometry_resources",
                    "mesh_variant_resources",
                    "retained_mesh_source_bytes",
                    "owned_geometry_bytes",
                ):
                    assert entry.session.status()[name] == before[name]
                self.record("Dedicated panel shares the native displaced mesh and original source", entry)
                self.stage = 1
                with hou.undos.group("Change Cycles displacement height"):
                    self.offset.parmTuple("vector").set((0, 0, 0.5))
            elif self.stage in (1, 2, 3):
                depth = 4.7 if self.stage == 2 else 4.5
                if not self.image(entry, depth):
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record(
                    (
                        "Material edit restores source vertices",
                        "Undo restores displacement",
                        "Redo restores displacement",
                    )[self.stage - 1],
                    entry,
                )
                if self.stage == 1:
                    self.stage = 2
                    hou.undos.performUndo()
                elif self.stage == 2:
                    self.stage = 3
                    hou.undos.performRedo()
                else:
                    self.stage = 4
                    self.builder.parm("cycles_displacement_method").set("bump")
            elif self.stage in (4, 5):
                if not self.image(entry, 5 if self.stage == 4 else 4.5):
                    return
                self.record(
                    "Bump mode restores original mesh" if self.stage == 4 else "True mode restores displaced mesh",
                    entry,
                )
                if self.stage == 4:
                    self.stage = 5
                    self.builder.parm("cycles_displacement_method").set("true")
                else:
                    self.stage = 6
                    self.camera.parm("tz").set(6)
                    self.rebuilds = entry.session.status()["displacement_rebuilds"]
            elif self.stage == 6:
                if not self.image(entry, 5.5):
                    return
                assert entry.session.status()["displacement_rebuilds"] == self.rebuilds
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("Camera edit retains displaced geometry", entry)
                self.stage = 7
                self.grid.parmTuple("size").set((1.2, 1.2))
            elif self.stage == 7:
                if not self.image(entry, 5.5):
                    return
                assert entry.session.status()["geometry_conversions"] > self.conversions
                self.record("SOP geometry edit displaces the new source once", entry)
                self.stage = 8
                self.output.setNamedInput("displacement", self.emission, "emission")
            elif self.stage == 8:
                if not failed_update(entry, self.generation, "float3 vector"):
                    return
                self.record("Invalid displacement type preserves the last valid image", entry)
                self.stage = 9
                self.output.setNamedInput("displacement", self.offset, "displacement")
            elif self.stage == 9:
                if not self.image(entry, 5.5):
                    return
                self.record("Rewiring recovers native displacement", entry)
                self.stage = 10
                self.offset.setName("renamed_offset")
            elif self.stage == 10:
                if not self.image(entry, 5.5):
                    return
                self.record("Renaming the displacement node preserves its stable graph identity", entry)
                self.panel.poll()
                path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "displacement-panel.png"
                assert self.panel.grab().save(str(path))
                self.report["panel_preview"] = str(path)
                self.viewer.killRender()
                self.viewer.close()
                self.stage = 11
            elif self.stage == 11:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Panel retains ownership after native viewer closure", entry)
                self.panel.render()
                self.stage = 12
            elif self.stage == 12:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation or not frame["complete"]:
                    return
                self.record("Panel continues the same displaced scene", entry)
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
        entry = registry.get(self.key)
        registry.close_all()
        if entry:
            self.report["closed"] = entry.session.status()
            assert (
                self.report["closed"]["owned_geometry_bytes"] == self.report["closed"]["device_allocation_bytes"] == 0
            )
        self.report["result"] = "passed" if code == 0 else "failed"
        self.save()
        self.code = code
        self.exit_at = time.monotonic() + 0.5
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if time.monotonic() >= self.exit_at:
            hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(lambda: self.exit(self.code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = DisplacementLifecycle()
