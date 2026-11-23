# SPDX-License-Identifier: Apache-2.0
"""Actual native IPR and panel updates through nested typed material ports."""

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
from ipr_update_checks import failed_update


class SubnetLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/materials/subnets/native-material-subnets.hiplc"), suppress_save_prompt=True
        )
        hou.setFrame(1)
        self.rop = hou.node("/out/native_subnet_cycles")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.builder = hou.node("/mat/native_nested_surface")
        self.outer = self.builder.node("renamed_outer")
        self.inner = self.outer.node("renamed_inner")
        self.tint = self.outer.node("tint_input")
        self.gain = self.outer.node("renamed_gain_connector")
        self.surface = self.outer.node("surface_output")
        self.color = self.builder.node("external_color")
        self.scalar = self.builder.node("external_gain")
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0.0
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
            measured = self.viewer.pixel("C", 32, 32)[:3]
        except hou.OperationFailed:
            return False
        return all(abs(value - target) < 0.001 for value, target in zip(measured, expected))

    def record(self, name, entry):
        assert entry.session.status()["geometry_conversions"] == self.conversions
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
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Nested material IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, (0.4, 0.8, 1.6), newer=False):
                    return
                self.conversions = entry.session.status()["geometry_conversions"]
                assert entry.last_soho["host_pid"] == os.getpid()
                self.record("Nested defaults render independently specified HDR radiance", entry)
                before_panel = entry.session.status()
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                after_panel = entry.session.status()
                for name in (
                    "generation",
                    "geometry_resources",
                    "material_resources",
                    "engine_shader_resources",
                    "owned_geometry_bytes",
                ):
                    assert after_panel[name] == before_panel[name], (name, before_panel, after_panel)
                # GPU work-tile buffers may grow while progressive samples run.
                # Join the render before testing unchanged allocation ownership.
                entry.session.cancel()
                quiescent = entry.session.status()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                assert entry.session.status()["device_allocation_bytes"] == quiescent["device_allocation_bytes"]
                self.report["panel_memory"] = dict(
                    active_before=before_panel,
                    active_after=after_panel,
                    quiescent_before=quiescent,
                    quiescent_after=entry.session.status(),
                )
                self.record("Both viewers share one nested material session", entry)
                self.advance(1, entry)
                self.tint.parmTuple("colordef").set((0.3, 0.2, 0.1))
            elif self.stage == 1:
                if not self.image(entry, (0.6, 0.4, 0.2)):
                    return
                self.record("Nested input default edit updates without geometry conversion", entry)
                self.advance(2, entry)
                self.gain.parm("floatdef").setExpression("$F + 1", hou.exprLanguage.Hscript)
                ch.promote_parameter(self.outer, self.gain, "floatdef", name="gain", label="Nested Gain")
                hou.setFrame(3)
            elif self.stage == 2:
                if not self.image(entry, (1.2, 0.8, 0.4)):
                    return
                self.record("Animated promoted subnet default follows frame changes", entry)
                self.advance(3, entry)
                with hou.undos.group("External nested port overrides"):
                    self.scalar.parm("value").set(5.0)
                    self.outer.setNamedInput("gain", self.scalar, "value")
                    self.outer.setNamedInput("tint", self.color, "color")
            elif self.stage == 3:
                if not self.image(entry, (0.5, 1.5, 2.5)):
                    return
                self.record("External values override promoted nested defaults", entry)
                self.advance(4, entry)
                hou.undos.performUndo()
            elif self.stage == 4:
                if not self.image(entry, (1.2, 0.8, 0.4)):
                    return
                self.record("Nested interface override undo", entry)
                self.advance(5, entry)
                hou.undos.performRedo()
            elif self.stage == 5:
                if not self.image(entry, (0.5, 1.5, 2.5)):
                    return
                self.record("Nested interface override redo", entry)
                self.advance(6, entry)
                self.surface.setInput(0, None)
            elif self.stage == 6:
                if not failed_update(entry, self.generation, "Connect a shader value"):
                    return
                self.record("Disconnected nested output reports a failed edit and retains the image", entry)
                self.stage = 7
                self.surface.setNamedInput(self.surface.inputNames()[0], self.inner, "surface")
            elif self.stage == 7:
                if not self.image(entry, (0.5, 1.5, 2.5)):
                    return
                self.record("Nested output rewiring automatically recovers both viewers", entry)
                self.advance(8, entry)
                self.gain.parm("parmtype").set(self.gain.parm("parmtype").parmTemplate().menuItems().index("int"))
            elif self.stage == 8:
                if not failed_update(entry, self.generation, "Unsupported Cycles subnet port"):
                    return
                self.record("Unsupported nested interface type is visible before mutation", entry)
                self.stage = 9
                self.gain.parm("parmtype").set(0)
            elif self.stage == 9:
                if not self.image(entry, (0.5, 1.5, 2.5)):
                    return
                self.record("Restoring the interface type recovers the material", entry)
                self.advance(10, entry)
                self.inner.setName("live_renamed_inner")
            elif self.stage == 10:
                if not self.image(entry, (0.5, 1.5, 2.5)):
                    return
                self.record("Nested rename preserves rendering state", entry)
                self.panel.poll()
                preview = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "nested-material-panel.png"
                assert self.panel.grab().save(str(preview))
                self.report["panel_preview"] = str(preview)
                self.viewer.killRender()
                self.viewer.close()
                self.stage = 11
            elif self.stage == 11:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Native viewer closes while the panel retains the nested session", entry)
                self.panel.render()
                self.stage = 12
            elif self.stage == 12:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                self.record("Dedicated panel continues after native viewer closure", entry)
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


test = SubnetLifecycle()
