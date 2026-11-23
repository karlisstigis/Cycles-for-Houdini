# SPDX-License-Identifier: Apache-2.0
"""Native ramp editing, promotion, interpolation failures and shared-view IPR."""

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


class RampLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/materials/ramps/native-material-ramps.hiplc"), suppress_save_prompt=True
        )
        hou.setFrame(1)
        self.rop = hou.node("/out/native_ramp_cycles")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.builder = hou.node("/mat/native_ramp_surface")
        self.ramp = self.builder.node("renamed_color_ramp")
        self.factor = self.builder.node("factor")
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
                raise TimeoutError("Native ramp IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, (1.0, 0.6, 0.9), newer=False):
                    return
                self.conversions = entry.session.status()["geometry_conversions"]
                assert entry.last_soho["host_pid"] == os.getpid()
                self.record("Nonuniform ramp renders independent HDR reference", entry)
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
                self.record("Panel and native viewer share one ramp session", entry)
                self.control = ch.promote_parameter(
                    self.builder, self.ramp, "ramp", name="palette", label="Emission Palette"
                )[0]
                self.advance(1, entry)
                self.control.set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((0.1, 0.2, 0.7), (0.7, 0.4, 0.1))))
            elif self.stage == 1:
                if not self.image(entry, (0.455, 0.485, 1.145)):
                    return
                self.record("Whole RGB ramp promotion follows edited values", entry)
                self.advance(2, entry)
                with hou.undos.group("Native ramp key edit"):
                    self.control.set(
                        hou.Ramp(
                            (hou.rampBasis.Constant, hou.rampBasis.Linear, hou.rampBasis.Linear),
                            (0.0, 0.2, 1.0),
                            ((0.1, 0.2, 0.3), (0.4, 0.6, 0.8), (0.9, 0.1, 0.2)),
                        )
                    )
            elif self.stage == 2:
                if not self.image(entry, (0.815625, 1.184375, 1.58125)):
                    return
                self.record("Key-count and interpolation edit updates both viewers", entry)
                self.advance(3, entry)
                hou.undos.performUndo()
            elif self.stage == 3:
                if not self.image(entry, (0.455, 0.485, 1.145)):
                    return
                self.record("Native ramp undo restores the curve", entry)
                self.advance(4, entry)
                hou.undos.performRedo()
            elif self.stage == 4:
                if not self.image(entry, (0.815625, 1.184375, 1.58125)):
                    return
                self.record("Native ramp redo restores added keys", entry)
                self.advance(5, entry)
                self.factor.parm("value").set(0.1)
            elif self.stage == 5:
                if not self.image(entry, (0.2, 0.4, 0.6)):
                    return
                self.record("Constant segment holds the independently specified left value", entry)
                self.advance(6, entry)
                self.builder.parm("palette2cr").setExpression("$F * .1 + .2", hou.exprLanguage.Hscript)
                self.factor.parm("value").set(0.2)
                hou.setFrame(3)
            elif self.stage == 6:
                if not self.image(entry, (1.0, 1.2, 1.6)):
                    return
                self.record("Animated promoted color knot follows frame changes", entry)
                self.advance(7, entry)
                self.builder.parm("palette3pos").set(0.2)
            elif self.stage == 7:
                if not recovered_update(entry, self.generation, "strictly increasing"):
                    return
                self.record("Duplicate keys report a warning and render the diagnostic material", entry)
                self.stage = 8
                self.builder.parm("palette3pos").set(0.9)
            elif self.stage == 8:
                if not self.image(entry, (1.0, 1.2, 1.6)):
                    return
                self.record("Correcting key order recovers the shared render", entry)
                self.advance(9, entry)
                self.builder.parm("palette1interp").set(2)
            elif self.stage == 9:
                if not self.image(entry, (1.0, 1.2, 1.6)):
                    return
                assert not entry.session.status()["source_diagnostics"]
                self.record("Native Catmull-Rom interpolation updates without diagnostic shading", entry)
                self.stage = 10
                self.builder.parm("palette1interp").set(0)
            elif self.stage == 10:
                if not self.image(entry, (1.0, 1.2, 1.6)):
                    return
                self.record("Supported interpolation restores automatic rendering", entry)
                self.advance(11, entry)
                self.ramp.setName("live_renamed_ramp")
            elif self.stage == 11:
                if not self.image(entry, (1.0, 1.2, 1.6)):
                    return
                self.record("Ramp rename preserves compiled graph identity", entry)
                self.panel.poll()
                preview = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "native-ramp-panel.png"
                assert self.panel.grab().save(str(preview))
                self.report["panel_preview"] = str(preview)
                self.viewer.killRender()
                self.viewer.close()
                self.stage = 12
            elif self.stage == 12:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Native viewer closes while the panel retains ramp ownership", entry)
                self.panel.render()
                self.stage = 13
            elif self.stage == 13:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                self.record("Panel continues the same ramp render after native viewer closure", entry)
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


test = RampLifecycle()
