# SPDX-License-Identifier: Apache-2.0
"""Run in an isolated Houdini UI using only public HOM and Qt lifecycle APIs."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
import cycles_houdini as ch
from cycles_houdini import registry


class LifecycleTest:
    def __init__(self):
        device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
        self.report = dict(houdini=hou.applicationVersionString(), host_pid=os.getpid(), device=device, events=[])
        self.started = time.monotonic()
        self.stage = 0
        self.generation = 0
        self.conversions = 0
        self.geometry = hou.node("/obj").createNode("geo", "cycles_ipr_geometry", run_init_scripts=False)
        self.box = self.geometry.createNode("box", "source")
        self.box.setRenderFlag(True)
        self.box.setDisplayFlag(True)
        # The lifecycle needs a visible red image, independent of the neutral
        # material and low-power native light defaults of a newly created ROP.
        self.material = ch.create_material(name="cycles_ipr_red", shader="diffuse_bsdf")
        self.material.node("surface_shader").parmTuple("color").set((0.8, 0.08, 0.03))
        self.geometry.parm("shop_materialpath").set(self.material.path())
        self.camera = hou.node("/obj").createNode("cam", "cycles_ipr_camera")
        self.camera.parm("tz").set(5)
        self.light = hou.node("/obj").createNode("hlight", "cycles_ipr_light")
        self.light.parmTuple("t").set((-2, 3, 4))
        self.light.parm("light_intensity").set(100)
        self.rop = hou.node("/out").createNode("cycles_ipr", "cycles_ipr_test")
        set_rop_devices(self.rop, device)
        for name, value in dict(
            source_mode="objects",
            vobject=self.geometry.path(),
            soppath=self.box.path(),
            camera=self.camera.path(),
            lightpath=self.light.path(),
            resx=320,
            resy=180,
            samples=65536,
            ipr_samples=1048576,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            world_source="color",
        ).items():
            self.rop.parm(name).set(value)
        self.rop.parmTuple("background").set((0.005, 0.005, 0.005))
        self.key = f"rop:{self.rop.sessionId()}"
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self._last_tick = 0
        self._tick_callback = self.tick
        hou.ui.addEventLoopCallback(self._tick_callback)
        self.viewer.startRender()
        self.last_report = 0
        self.save()

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

    def save(self):
        path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def tick(self):
        if time.monotonic() - self._last_tick < 0.1:
            return
        self._last_tick = time.monotonic()
        try:
            if time.monotonic() - self.started > 80:
                raise TimeoutError(f"Native IPR stalled at stage {self.stage}; ROP errors: {self.rop.errors()}")
            entry = registry.get(self.key)
            if time.monotonic() - self.last_report > 1:
                self.last_report = time.monotonic()
                self.report["current"] = dict(
                    stage=self.stage,
                    active=self.viewer.isActive(),
                    rendering=self.viewer.isRendering(),
                    preview=self.viewer.isPreviewOn(),
                    entry=entry.session.status() if entry else None,
                    helper_pid=entry.helper_pid if entry else None,
                    soho=list(registry.debug_events),
                    rop_errors=self.rop.errors(),
                )
                try:
                    self.report["current"]["resolution"] = self.viewer.imageResolution()
                    self.report["current"]["pixel"] = self.viewer.pixel("C", 160, 90)
                except hou.OperationFailed as error:
                    self.report["current"]["image_error"] = str(error)
                self.save()
            # This ordered lifecycle regression verifies accepted renderer
            # state. A queued generation denotes desired state before its ACK;
            # it cannot prove that deletion/undo has reached GPU ownership yet.
            if entry and entry.session.status().get("pending_update"):
                return
            if self.stage == 0:
                if not entry or not entry.helper_pid:
                    return
                try:
                    size = self.viewer.imageResolution()
                    centre = self.viewer.pixel("C", 160, 90)
                except hou.OperationFailed:
                    return
                if size != (320, 180) or centre[0] <= centre[1] * 2 or centre[0] < 1e-4:
                    return
                assert entry.last_soho["mode"] == "generate"
                assert entry.last_soho["host_pid"] == os.getpid()
                assert entry.helper_pid != os.getpid()
                assert not entry.session.frame()["complete"], "Native IPR received no unfinished render"
                self.entry = entry
                self.generation = entry.session.status()["generation"]
                self.conversions = entry.session.status()["geometry_conversions"]
                self.report["initial_pixel"] = list(centre)
                self.report["planes"] = list(self.viewer.planes())
                self.record("native image", entry)
                status = entry.session.status()
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                interface = hou.pypanel.interfaceByName("cycles_houdini_render")
                assert interface is not None, "Development package did not discover the Cycles Python Panel"
                self.panel_tab.setActiveInterface(interface)
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and entry.views == 1
                assert len(registry.entries()) == 1
                assert entry.session.status()["generation"] == status["generation"]
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
                self.record("panel shares native renderer without new device allocations", entry)
                self.camera.parm("tx").set(0.1)
                self.stage = 1
            elif self.stage == 1:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert entry.last_soho["mode"] == "update"
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("camera update without geometry conversion", entry)
                self.viewer.pauseRender()
                self.stage = 2
            elif self.stage == 2:
                if not self.viewer.isPaused() or entry.session.status()["state"] != "paused":
                    return
                self.record("native pause", entry)
                self.panel.pause(False)
                self.stage = 3
            elif self.stage == 3:
                if self.viewer.isPaused() or entry.session.status()["state"] == "paused":
                    return
                self.record("native resume", entry)
                self.generation = entry.session.status()["generation"]
                if os.environ.get("CYCLES_HOUDINI_TEST_SKIP_REGION") == "1":
                    self.box.parm("sizex").set(1.4)
                    self.stage = 4
                else:
                    self.panel.set_region((80, 45, 160, 90))
                    self.stage = 31
            elif self.stage == 31:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                assert (frame["x"], frame["y"], frame["width"], frame["height"]) == (80, 45, 160, 90)
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("shared region update", entry)
                self.generation = frame["generation"]
                self.panel.reset_region()
                self.stage = 32
            elif self.stage == 32:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                assert (frame["x"], frame["y"], frame["width"], frame["height"]) == (0, 0, 320, 180)
                self.record("shared region reset", entry)
                self.generation = frame["generation"]
                self.box.parm("sizex").set(1.4)
                self.stage = 4
            elif self.stage == 4:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions + 1
                self.record("SOP update", entry)
                self.generation = entry.session.status()["generation"]
                self.light.parm("tx").set(-1)
                self.stage = 5
            elif self.stage == 5:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions + 1
                self.record("light update", entry)
                self.conversions = entry.session.status()["geometry_conversions"]
                self.generation = entry.session.status()["generation"]
                self.geometry.parm("tx").set(0.1)
                self.stage = 50
            elif self.stage == 50:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("OBJ transform without geometry conversion", entry)
                self.generation = entry.session.status()["generation"]
                self.box.setName("source_renamed")
                self.stage = 51
            elif self.stage == 51:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert self.rop.evalParm("soppath") == self.box.path(), "ROP reference did not follow SOP rename"
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("SOP rename retains geometry identity", entry)
                self.generation = entry.session.status()["generation"]
                self.light_path = self.light.path()
                with hou.undos.group("Cycles lifecycle light deletion"):
                    self.light.destroy()
                self.stage = 52
            elif self.stage == 52:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert not entry.source_lights, "Deleted light remained in the scene"
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("light deletion removes engine resource", entry)
                self.generation = entry.session.status()["generation"]
                assert hou.undos.performUndo()
                self.stage = 53
            elif self.stage == 53:
                if entry.session.status()["generation"] <= self.generation:
                    return
                self.light = hou.node(self.light_path)
                assert self.light is not None and len(entry.source_lights) == 1
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("undo restores light through network notifications", entry)
                self.generation = entry.session.status()["generation"]
                self.sop_path = self.box.path()
                with hou.undos.group("Cycles lifecycle SOP deletion"):
                    self.box.destroy()
                self.stage = 54
            elif self.stage == 54:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert not entry.source_objects and entry.session.status()["owned_geometry_bytes"] == 0
                self.record("SOP deletion retires object and geometry", entry)
                self.generation = entry.session.status()["generation"]
                assert hou.undos.performUndo()
                self.stage = 55
            elif self.stage == 55:
                if entry.session.status()["generation"] <= self.generation:
                    return
                self.box = hou.node(self.sop_path)
                assert self.box is not None and entry.source_objects
                assert entry.session.status()["geometry_conversions"] == self.conversions + 1
                self.conversions += 1
                self.record("undo restores SOP with fresh owned geometry", entry)
                self.generation = entry.session.status()["generation"]
                self.box.parm("sizex").setExpression("1 + ($F - 1) * 0.01", hou.exprLanguage.Hscript)
                self.stage = 56
            elif self.stage == 56:
                if entry.session.status()["generation"] <= self.generation:
                    return
                self.conversions = entry.session.status()["geometry_conversions"]
                self.generation = entry.session.status()["generation"]
                hou.setFrame(hou.frame() + 1)
                self.stage = 57
            elif self.stage == 57:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions + 1
                self.conversions += 1
                self.record("timeline edit recooks animated geometry", entry)
                self.generation = entry.session.status()["generation"]
                self.viewer.setAutoUpdate(False)
                self.camera.parm("tx").set(0.2)
                self.manual_started = time.monotonic()
                self.stage = 58
            elif self.stage == 58:
                if time.monotonic() - self.manual_started < 0.5:
                    return
                assert entry.session.status()["generation"] == self.generation, "Automatic fallback ignored manual mode"
                self.record("manual update mode preserves generation", entry)
                self.viewer.setAutoUpdate(True)
                self.stage = 59
            elif self.stage == 59:
                if entry.session.status()["generation"] <= self.generation:
                    return
                assert entry.session.status()["geometry_conversions"] == self.conversions
                self.record("automatic updates consume pending camera change", entry)
                self.reload_path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).with_suffix(".hiplc")
                self.rop_path, self.camera_path = self.rop.path(), self.camera.path()
                hou.hipFile.save(str(self.reload_path))
                self.viewer.setAutoUpdate(False)
                prior = entry
                hou.hipFile.load(str(self.reload_path), suppress_save_prompt=True, ignore_load_warnings=True)
                assert not registry.entries()
                assert (
                    prior.session.status()["state"] == "closed"
                    and prior.session.status()["device_allocation_bytes"] == 0
                )
                self.report["reload"] = "active renderer closed before scene replacement"
                self.rop = hou.node(self.rop_path)
                self.camera = hou.node(self.camera_path)
                self.box = hou.node(self.rop.evalParm("soppath"))
                self.geometry = self.box.parent()
                self.light = hou.node(self.rop.evalParm("lightpath"))
                self.key = f"rop:{self.rop.sessionId()}"
                self.viewer.setRopNode(self.rop)
                self.viewer.setAutoUpdate(True)
                self.viewer.startRender()
                self.stage = 60
            elif self.stage == 60:
                if entry is None or not entry.helper_pid or entry.session.frame_info() is None:
                    return
                assert entry is not self.entry
                current_panel = self.panel_tab.activeInterfaceRootWidget()
                live_panels = []
                for pane in hou.ui.paneTabs():
                    if pane.type() != hou.paneTabType.PythonPanel:
                        continue
                    widget = pane.activeInterfaceRootWidget()
                    if widget is not None and hasattr(widget, "_disposed") and hasattr(widget, "entry"):
                        live_panels.append(
                            dict(
                                pane=pane.name(),
                                stored_widget=widget is self.panel,
                                current_widget=widget is current_panel,
                                disposed=widget._disposed,
                                shares_entry=widget.entry is entry,
                            )
                        )
                self.report["panel_reload"] = dict(
                    stored_is_current=self.panel is current_panel,
                    stored_disposed=self.panel._disposed,
                    stored_shares_entry=self.panel.entry is entry,
                    views_before=entry.views,
                    live_panels=live_panels,
                )
                self.save()
                self.entry = entry
                self.panel.set_rop(self.rop)
                self.report["panel_reload"].update(
                    views_after=entry.views,
                    stored_shares_entry_after=self.panel.entry is entry,
                    stored_info=self.panel.info.text(),
                )
                self.save()
                assert self.panel.entry is entry and entry.views == 1
                self.record("native render and shared panel restart after scene reload", entry)
                self.report["kill_requested"] = time.monotonic() - self.started
                self.save()
                self.viewer.killRender()
                self.stage = 6
            elif self.stage == 6:
                if entry and entry.channel:
                    return
                assert entry is self.entry and entry.views == 1
                if entry.session.status()["state"] != "cancelled":
                    return  # Pipe closure precedes the asynchronous Stop ACK.
                assert entry.session.status()["state"] == "cancelled"
                self.record("native stop cancels shared render while panel retains session", entry)
                self.panel.poll()
                self.panel.grab().save(str(Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).with_suffix(".png")))
                self.panel_tab.close()
                self.stage = 7
            elif self.stage == 7:
                if entry:
                    return
                if self.entry.session.status()["state"] != "closed":
                    return  # Registry retirement precedes the queued Close ACK.
                assert self.entry.session.status()["device_allocation_bytes"] == 0
                self.report["kill"] = "renderer closed and device allocations released"
                self.report["result"] = "passed"
                self.save()
                self.finish(0)
        except Exception:
            self.report["result"] = "failed"
            self.report["stage"] = self.stage
            self.report["error"] = traceback.format_exc()
            self.save()
            self.finish(1)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self._tick_callback)
        self.viewer.killRender()
        if hasattr(self, "panel"):
            self.panel.dispose()
            try:
                self.panel_tab.close()
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
        registry.close_all()
        self._exit_code = code
        self._exit_at = time.monotonic() + 0.5
        self._shutdown_callback = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown_callback)

    def shutdown(self):
        if time.monotonic() >= self._exit_at:
            hou.ui.removeEventLoopCallback(self._shutdown_callback)
            # Run application shutdown from the host loop, outside a Qt signal
            # dispatch that may still be using a floating pane's receiver.
            hou.ui.postEventCallback(lambda: self.exit(self._exit_code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass  # hou.exit raises after scheduling normal application shutdown.


test = LifecycleTest()
