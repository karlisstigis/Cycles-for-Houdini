# SPDX-License-Identifier: Apache-2.0
"""Actual native volume-graph IPR, field errors and shared-panel ownership."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import time
import traceback
from array import array

import hou
import cycles_houdini as ch
from cycles_houdini import registry
from ipr_update_checks import failed_update


class VolumeMaterialLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        destination = artifacts_root() / "stage3/materials/volumes"
        hou.hipFile.load(str(destination / "native-volume-materials.hiplc"), suppress_save_prompt=True)
        hou.setFrame(1)
        self.rop = hou.node("/out/native_volume_material_cycles")
        self.obj = hou.node("/obj/volume_material_receiver")
        self.stash = hou.node(self.rop.evalParm("soppath"))
        self.missing = self.obj.node("missing_temperature")
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.absorb = hou.node("/mat/native_absorption")
        self.emission = hou.node("/mat/native_field_emission")
        self.principled = hou.node("/mat/native_pyro")
        self.p = self.principled.node("volume_shader")
        self.obj.parm("shop_materialpath").set(self.absorb.path())
        self.absorb.node("density_scale").parm("value2").set(1.0)
        # This lifecycle explicitly swaps the SOP source and uses the Interactive
        # profile. Do not depend on the artist-facing Objects/profile defaults.
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                source_mode="sop",
                resx=80,
                resy=48,
                resolution_percent=100,
                ipr_resolution_percent=0,
                viewer_interactive=1,
                samples=512,
                adaptive_sampling=0,
                use_denoise=0,
                ipr_samples=512,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
            )
        )
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(
            device=rop_devices(self.rop, hou.time())[0],
            host_pid=os.getpid(),
            events=[],
            color_policy=ch.color_policy(),
            scope="Live graph lifecycle in the GUI active working space. Exact native/reference image comparisons are a separate matched-config test.",
        )
        self.started = time.monotonic()
        self.last_tick = 0.0
        self.stage = 0
        self.generation = 0
        self.finished = False
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

    def image(self, entry, expected=None, tolerance=0.025, newer=True):
        if not entry or entry.error:
            return False
        state = entry.session.status()
        frame = entry.session.frame_info()
        if newer and state["generation"] <= self.generation:
            return False
        if not frame or frame["generation"] != state["generation"]:
            return False
        try:
            pixels = [self.viewer.pixel("C", x, y)[:3] for y in range(21, 27) for x in range(37, 43)]
        except hou.OperationFailed:
            return False
        measured = [sum(pixel[channel] for pixel in pixels) / len(pixels) for channel in range(3)]
        self.report["measured"] = measured
        if expected is None:
            # Compare native pixels to the current raw core image so a stale
            # viewer tile cannot become the warm/hot lifecycle reference.
            raw = entry.session.frame()
            if not raw or raw["generation"] != state["generation"]:
                return False
            values = array("f")
            values.frombytes(raw["planes"]["combined"]["pixels"])
            expected = [
                sum(values[(y * 80 + x) * 4 + channel] for y in range(21, 27) for x in range(37, 43)) / 36
                for channel in range(3)
            ]
        if not all(math.isfinite(value) for value in measured + list(expected)):
            return False
        return max(abs(a - b) for a, b in zip(measured, expected)) < tolerance

    def record(self, name, entry, reuse=False):
        if reuse:
            state = entry.session.status()
            assert state["geometry_conversions"] == self.conversions
            assert state["material_binding_scans"] == self.scans
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                measured=self.report.get("measured"),
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

    def failed(self, entry, fragment):
        return failed_update(entry, self.generation, fragment)

    def tick(self):
        if self.finished:
            return
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
                raise TimeoutError(f"Native volume-material IPR stalled at stage {self.stage}")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, [math.exp(-9 / 8)] * 3, newer=False):
                    return
                assert entry.last_soho["host_pid"] == os.getpid() and entry.session.status()["geometry_resources"] == 1
                self.record("Native volume graph matches independent absorption integral", entry)
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
                assert (
                    self.panel.entry is entry
                    and entry.session.status()["device_allocation_bytes"] == joined["device_allocation_bytes"]
                )
                self.report["panel_memory"] = dict(active_before=before, active_after=active, joined=joined)
                self.record("Native viewer and panel share sparse resources and quiescent allocations", entry)
                self.advance(1, entry)
                with hou.undos.group("Volume density multiplier"):
                    self.absorb.node("density_scale").parm("value2").set(2.0)
            elif self.stage == 1:
                if not self.image(entry, [math.exp(-9 / 4)] * 3):
                    return
                self.record("Referenced volume shader edit retains grids and bindings", entry, reuse=True)
                self.advance(2, entry)
                hou.undos.performUndo()
            elif self.stage == 2:
                if not self.image(entry, [math.exp(-9 / 8)] * 3):
                    return
                self.record("Volume shader undo restores the density", entry, reuse=True)
                self.advance(3, entry)
                hou.undos.performRedo()
            elif self.stage == 3:
                if not self.image(entry, [math.exp(-9 / 4)] * 3):
                    return
                self.record("Volume shader redo restores the density", entry, reuse=True)
                self.advance(4, entry)
                self.camera.parm("tx").set(0.05)
            elif self.stage == 4:
                if not self.image(entry, [math.exp(-9 / 4)] * 3):
                    return
                self.record("Volume camera change retains owned sparse grids", entry, reuse=True)
                self.advance(5, entry)
                self.obj.parm("shop_materialpath").set(self.emission.path())
                self.rop.parmTuple("background").set((0, 0, 0))
            elif self.stage == 5:
                if not self.image(entry, [0.45, 0.1125, 0.05625]):
                    return
                self.record("OBJ reassignment switches to a custom emission-field graph", entry)
                self.advance(6, entry)
                self.emission.node("emission_scale").parm("value2").set(1.0)
            elif self.stage == 6:
                if not self.image(entry, [0.225, 0.05625, 0.028125]):
                    return
                self.record("Custom field strength edit retains grids and bindings", entry, reuse=True)
                self.advance(7, entry)
                self.obj.parm("shop_materialpath").set(self.principled.path())
                self.p.parm("density").set(0.0)
                self.p.parm("blackbody_intensity").set(1.0)
                self.p.parm("temperature").set(1000.0)
            elif self.stage == 7:
                # Blackbody RGB depends on the active scene-linear role. The
                # isolated GUI preferences may select a different OCIO config
                # from hython, so retain a local warm image for this lifecycle.
                if not self.image(entry):
                    return
                self.warm = self.report["measured"]
                if not (self.warm[0] > self.warm[1] > self.warm[2] >= 0 and sum(self.warm) > 0.1):
                    return
                self.record("Named temperature drives progressive blackbody emission", entry)
                self.advance(8, entry)
                self.rop.parm("soppath").set(self.missing.path())
            elif self.stage == 8:
                if not self.image(entry, [0, 0, 0]):
                    return
                assert any(
                    d["code"] == "volume.shader_field_unavailable" for d in entry.session.status()["source_diagnostics"]
                )
                self.record("Missing temperature renders a non-emitting fallback and keeps IPR active", entry)
                self.stage = 9
                self.rop.parm("soppath").set(self.stash.path())
                self.p.parm("temperature").set(1500.0)
            elif self.stage == 9:
                if not self.image(entry):
                    return
                self.hot = self.report["measured"]
                if not (self.hot[0] > self.hot[1] > self.hot[2] >= 0 and sum(self.hot) > 2 * sum(self.warm)):
                    return
                self.record("Restoring the required field recovers the same session", entry)
                self.advance(10, entry)
                self.invalid = self.principled.createNode("cycles::diffuse_bsdf::1", "wrong_volume_domain")
                self.principled.node("volume_output").setNamedInput("volume", self.invalid, "BSDF")
            elif self.stage == 10:
                if not self.image(entry, [0, 0, 0]):
                    return
                assert any(
                    d["code"] == "material.volume_source_unavailable"
                    for d in entry.session.status()["source_diagnostics"]
                )
                self.record("Invalid closure domain renders a non-emitting fallback", entry)
                self.stage = 11
                self.principled.node("volume_output").setNamedInput("volume", self.p, "volume")
                self.invalid.destroy()
            elif self.stage == 11:
                if not self.image(entry, self.hot, tolerance=0.1):
                    return
                self.record("Corrected volume graph restores shading after fallback", entry)
                self.advance(12, entry)
                self.principled.setName("renamed_native_pyro")
            elif self.stage == 12:
                if not self.image(entry, self.hot, tolerance=0.1):
                    return
                assert self.obj.evalParm("shop_materialpath") == self.principled.path()
                assert any(
                    reference["path"] == self.principled.path()
                    for reference in entry.session.native_material_references()
                )
                self.record("Volume builder rename follows native path references", entry)
                self.advance(13, entry)
                self.panel.poll()
                preview = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "native-volume-material-panel.png"
                assert self.panel.grab().save(str(preview))
                self.report["panel_preview"] = str(preview)
                self.viewer.killRender()
                self.viewer.close()
            elif self.stage == 13:
                if entry.helper_pid or entry.channel:
                    return
                assert registry.get(self.key) is entry and entry.views == 1
                self.record("Panel retains volume graph after native viewer closure", entry)
                self.stage = 14
                self.panel.render()
            elif self.stage == 14:
                frame = entry.session.frame_info()
                if not frame or frame["generation"] <= self.generation:
                    return
                self.record("Panel continues the same native volume session", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        if self.finished:
            return
        self.finished = True
        self._exit_code = code
        if self._tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self._tick)
        entry = registry.get(self.key)
        self.retiring = entry.session if entry else None
        operations = [self.viewer.killRender, self.viewer.close]
        if hasattr(self, "panel"):
            operations.extend((self.panel.dispose, self.panel_tab.close))
        operations.append(registry.close_all)
        for operation in operations:
            try:
                operation()
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
            except Exception:
                self.report.setdefault("cleanup_errors", []).append(traceback.format_exc())
                self._exit_code = 1
        # Native IPR and the panel share an asynchronously retiring worker.
        # Keep the primary test error and wait before asserting released bytes.
        self.report["result"] = "retiring"
        self.save()
        self._closing_started = time.monotonic()
        self._exit_at = None
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if self._exit_at is None:
            try:
                if self.retiring is not None:
                    closed = self.retiring.status()
                    self.report["close"] = closed
                    if closed["state"] != "closed" and time.monotonic() - self._closing_started < 20:
                        return
                    if (
                        closed["state"] != "closed"
                        or closed["owned_geometry_bytes"]
                        or closed["device_allocation_bytes"]
                    ):
                        raise AssertionError(
                            "Volume IPR worker did not retire with zero tracked resources: " + str(closed)
                        )
            except Exception:
                self.report.setdefault("cleanup_errors", []).append(traceback.format_exc())
                self._exit_code = 1
            self.report["result"] = "passed" if self._exit_code == 0 else "failed"
            self.save()
            self._exit_at = time.monotonic() + 0.5
            return
        if time.monotonic() >= self._exit_at:
            if self._shutdown in hou.ui.eventLoopCallbacks():
                hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(lambda: self.exit(self._exit_code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = VolumeMaterialLifecycle()
