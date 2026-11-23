# SPDX-License-Identifier: Apache-2.0
"""Real native IPR and panel color reloads, manual updates, pause and cancel."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
from array import array

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import registry


class ColorLifecycle:
    def __init__(self):
        self.original = OCIO.GetCurrentConfig()
        self.original_env = hou.getenv("OCIO")
        self.portable_textures = os.getenv("CYCLES_HOUDINI_TEST_TEXTURE_PORTABLE") == "1"
        self.auto_textures = os.getenv("CYCLES_HOUDINI_TEST_TEXTURE_AUTO") == "1" or self.portable_textures
        self.original_mode = hou.getenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
        if self.auto_textures:
            hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "0" if self.portable_textures else "4")
        self.serial = 0
        self.destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(str(artifacts_root() / "stage3/textures/native-textures.hiplc"), suppress_save_prompt=True)
        self.rop = hou.node("/out/native_texture_cycles")
        self.texture = hou.node("/mat/native_texture_surface/file_image")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                samples=16777216,
                ipr_samples=16777216,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
            )
        )
        # Older saved fixtures predate required-coordinate validation. Supply
        # explicit coordinates here; this test measures color, not UV recovery.
        source = self.rop.node(self.rop.evalParm("soppath"))
        assert source is not None, "Color IPR fixture has no geometry source"
        uv = source.parent().createNode("attribwrangle", "color_ipr_coordinates")
        uv.setInput(0, source)
        uv.parm("snippet").set("v@uv=set(0.5,0.5,0);")
        uv.setDisplayFlag(True)
        uv.setRenderFlag(True)
        assert uv.geometry().findPointAttrib("uv") is not None
        self.rop.parm("soppath").set(uv.path())
        self.rop.parm("source_mode").set("sop")
        self.rgb = [0.8, 0.4, 0.1]
        path = self.destination / "encoded.exr"
        output = OIIO.ImageOutput.create(str(path))
        spec = OIIO.ImageSpec(8, 8, 3, OIIO.FLOAT)
        if self.portable_textures:
            spec.attribute("colorInteropID", "srgb_rec709_scene")
        assert output.open(str(path), spec)
        assert output.write_image(np.broadcast_to(np.asarray(self.rgb, np.float32), (8, 8, 3)).copy())
        assert output.close()
        self.texture.parm("filename").set(str(path))
        self.texture.parm("colorspace").set("auto" if self.auto_textures else "sRGB Encoded Rec.709 (sRGB)")
        self.config = self.profile("ACEScg")
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(
            device=rop_devices(self.rop, hou.time())[0],
            host_pid=os.getpid(),
            auto_textures=self.auto_textures,
            portable_textures=self.portable_textures,
            events=[],
        )
        self.started = time.monotonic()
        self.last_tick = 0
        self.stage = 0
        self.busy = self.finished = False
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self._tick = self.tick
        hou.ui.addEventLoopCallback(self._tick)
        self.viewer.startRender()
        self.save()

    def profile(self, working, view=None):
        config = OCIO.Config.CreateFromStream(self.original.serialize())
        config.setWorkingDir(self.original.getWorkingDir())
        config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
        self.input_space = "sRGB Encoded Rec.709 (sRGB)"
        if self.auto_textures and not self.portable_textures:
            self.input_space = "sRGB Encoded Rec.709 (sRGB)" if working == "ACEScg" else "Linear Rec.709 (sRGB)"
            rules = config.getFileRules()
            rules.insertRule(0, "Native IPR Auto Input", self.input_space, "*", "exr")
            config.setFileRules(rules)
        config.validate()
        if view:
            config.setActiveViews(view)
        self.serial += 1
        path = self.destination / f"profile-{self.serial}.ocio"
        path.write_text(config.serialize(), encoding="utf-8")
        hou.putenv("OCIO", str(path))
        hou.Color.reloadOCIO()
        return OCIO.GetCurrentConfig()

    def expected(self):
        return (
            self.config.getProcessor(self.input_space, OCIO.ROLE_SCENE_LINEAR)
            .getDefaultCPUProcessor()
            .applyRGB(self.rgb)
        )

    def native_matches(self, entry, newer=False):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        assert not status["source_diagnostics"], status["source_diagnostics"]
        frame = entry.session.frame_info()
        if not frame or (
            newer and (status["generation"] <= self.generation or frame["generation"] != status["generation"])
        ):
            return False
        try:
            pixel = self.viewer.pixel("C", 32, 32)[:3]
        except hou.OperationFailed:
            return False
        core = entry.session.frame()
        values = array("f")
        values.frombytes(core["planes"]["combined"]["pixels"])
        offset = (32 * 64 + 32) * 4
        self.report["core_rgb"] = list(values[offset : offset + 3])
        if (
            not status["color_update_pending"]
            and frame["generation"] == status["generation"]
            and frame["working_space"] == self.config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
        ):
            if not np.allclose(values[offset : offset + 3], self.expected(), rtol=0, atol=0.001):
                return False
        # A stopped preview has the engine's actual accumulated/half precision.
        # Verify its color meaning from those retained pixels, rather than
        # treating long-running floating-point accumulation as an OCIO error.
        expected = self.expected()
        if status["color_update_pending"]:
            alpha = values[offset + 3]
            rgb = [v / alpha if alpha > 1e-8 else 0 for v in values[offset : offset + 3]]
            source = (
                self.original.getProcessor(frame["working_space"], "aces_interchange")
                .getDefaultCPUProcessor()
                .applyRGB(rgb)
            )
            expected = (
                self.config.getProcessor("aces_interchange", OCIO.ROLE_SCENE_LINEAR)
                .getDefaultCPUProcessor()
                .applyRGB(source)
            )
            expected = [v * alpha for v in expected]
        self.report["native_rgb"] = list(pixel)
        self.report["expected_native_rgb"] = list(expected)
        return np.allclose(pixel, expected, rtol=0, atol=0.001)

    def save(self):
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def record(self, name, entry):
        inputs = entry.session.texture_inputs()
        if self.auto_textures:
            assert self.texture.evalParm("colorspace") == "auto"
            if not entry.session.status()["color_update_pending"]:
                assert (
                    len(inputs) == 1
                    and inputs[0]["colorspace"] == self.config.getColorSpace(self.input_space).getName()
                ), inputs
                if self.portable_textures:
                    assert inputs[0]["selection"] == "colorInteropID", inputs
            self.panel.poll_texture_inputs(force=True)
            assert inputs[0]["colorspace"] in self.panel.texture_info.toPlainText()
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                frame=entry.session.frame_info(),
                texture_inputs=inputs,
                native_rgb=list(self.viewer.pixel("C", 32, 32)[:3]),
            )
        )
        self.save()

    def advance(self, stage, entry):
        self.generation = entry.session.status()["generation"]
        self.stage_at = time.monotonic()
        self.stage = stage

    def retained(self, entry):
        status = entry.session.status()
        assert status["geometry_conversions"] == self.conversions
        assert status["extractions"] == self.extractions
        assert len(registry.entries()) == 1 and self.panel.entry is entry
        assert not entry.error, entry.error

    def tick(self):
        if self.busy or self.finished:
            return
        if time.monotonic() - self.last_tick < 0.1:
            return
        self.last_tick = time.monotonic()
        self.busy = True
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage,
                error=entry.error if entry else "",
                status=entry.session.status() if entry else None,
                viewer_active=self.viewer.isActive(),
                viewer_paused=self.viewer.isPaused(),
                serials=[entry.change_serial, entry.applied_serial, entry.requested_serial, entry.color_notified]
                if entry
                else None,
            )
            self.save()
            if time.monotonic() - self.started > 180:
                raise TimeoutError("Native color IPR stalled at stage " + str(self.stage))
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.native_matches(entry):
                    return
                self.conversions = entry.session.status()["geometry_conversions"]
                self.extractions = entry.session.status()["extractions"]
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                if self.auto_textures:
                    self.panel.texture_details.setChecked(True)
                    assert self.input_space in self.panel.texture_info.toPlainText()
                self.record("Native IPR and dedicated panel share an ACEScg render", entry)
                policy = ch.color_policy()
                self.alternate = next(
                    v
                    for v in policy["views"][policy["default_display"]]
                    if v != policy["default_views"][policy["default_display"]]
                )
                self.advance(1, entry)
                self.config = self.profile("ACEScg", self.alternate)
            elif self.stage == 1:
                if self.panel._color_configuration_id != ch.color_policy()["configuration_id"]:
                    return
                self.retained(entry)
                assert entry.session.status()["generation"] == self.generation
                assert not entry.session.status()["color_update_pending"]
                assert [self.panel.view.itemText(i) for i in range(self.panel.view.count())] == [self.alternate]
                assert self.native_matches(entry)
                self.record("Display/view menus refresh without restarting tracing or reloading geometry", entry)
                self.viewer.setAutoUpdate(False)
                self.panel.auto.setChecked(False)
                self.advance(2, entry)
                self.config = self.profile("Linear Rec.709 (sRGB)")
            elif self.stage == 2:
                if not entry.session.status()["color_update_pending"] or not self.native_matches(entry):
                    return
                if time.monotonic() - self.stage_at < 2:
                    return
                self.retained(entry)
                assert entry.session.status()["generation"] == self.generation
                assert entry.session.status()["state"] == "idle"
                assert entry.session.frame_info()["working_space"] == "ACEScg"
                self.panel.poll()
                image = entry.session.display_image(
                    display=self.panel.display.currentText(), view=self.panel.view.currentText()
                )
                actual = self.panel.canvas.image.pixelColor(32, 31).getRgb()
                assert list(actual) == list(image["rgba"][(31 * 64 + 32) * 4 : (31 * 64 + 32) * 4 + 4]), actual
                self.record("Manual update preserves the old frame; both viewers use its source color meaning", entry)
                self.advance(3, entry)
                self.viewer.setAutoUpdate(True)
                self.panel.auto.setChecked(True)
            elif self.stage == 3:
                if not self.native_matches(entry, True):
                    return
                self.retained(entry)
                assert entry.session.frame_info()["working_space"] == "Linear Rec.709 (sRGB)"
                assert not entry.session.status()["color_update_pending"]
                self.record("Automatic update refreshes input textures in Rec.709 without a geometry recook", entry)
                self.viewer.pauseRender()
                self.panel.pause(True)
                self.advance(4, entry)
                self.config = self.profile("ACEScg")
            elif self.stage == 4:
                status = entry.session.status()
                if not status["color_update_pending"] or not self.native_matches(entry):
                    return
                if time.monotonic() - self.stage_at < 2:
                    return
                self.retained(entry)
                assert status["state"] == "paused" and self.viewer.isPaused()
                assert status["generation"] == self.generation
                self.record("Native pause retains the color update for resume and preserves old-frame display", entry)
                self.advance(5, entry)
                self.viewer.resumeRender()
                self.panel.pause(False)
            elif self.stage == 5:
                frame = entry.session.frame_info()
                if (
                    not frame
                    or entry.session.status()["generation"] <= self.generation
                    or frame["generation"] != entry.session.status()["generation"]
                    or not self.native_matches(entry)
                ):
                    return
                assert frame["working_space"] == "ACEScg"
                self.retained(entry)
                self.record("Resume traces the new color epoch", entry)
                self.panel.cancel()
                self.advance(6, entry)
                self.config = self.profile("Linear Rec.709 (sRGB)")
            elif self.stage == 6:
                if not entry.session.status()["color_update_pending"] or not self.native_matches(entry):
                    return
                if time.monotonic() - self.stage_at < 2:
                    return
                self.retained(entry)
                assert entry.session.status()["state"] == "cancelled"
                assert entry.session.status()["generation"] == self.generation
                self.record(
                    "Canceled render stays canceled across a color reload while the old image remains usable", entry
                )
                self.advance(7, entry)
                self.viewer.startRender()
            elif self.stage == 7:
                if not self.native_matches(entry, True):
                    return
                self.retained(entry)
                assert entry.session.frame_info()["working_space"] == "Linear Rec.709 (sRGB)"
                assert not entry.session.status()["color_update_pending"]
                self.record("An explicit native restart recovers the canceled session", entry)
                self.panel.poll()
                path = self.destination / "color-panel.png"
                assert self.panel.grab().save(str(path))
                self.report["panel_preview"] = str(path)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)
        finally:
            self.busy = False

    def finish(self, code):
        if self.finished:
            return
        self.finished = True
        self.code = code
        entry = registry.get(self.key)
        self.retiring = entry.session if entry else None
        if self._tick in hou.ui.eventLoopCallbacks():
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
        self.close_deadline = time.monotonic() + 30
        self._retire = self.finish_when_closed
        hou.ui.addEventLoopCallback(self._retire)
        self.save()

    def finish_when_closed(self):
        if self.retiring:
            self.report["closed"] = self.retiring.status()
            if self.report["closed"]["state"] != "closed":
                if time.monotonic() < self.close_deadline:
                    return
                self.code = 1
                self.report["error"] = self.report.get("error", "") + "\nColor IPR worker retirement timed out"
            elif self.report["closed"]["owned_geometry_bytes"] or self.report["closed"]["device_allocation_bytes"]:
                self.code = 1
                self.report["error"] = (
                    self.report.get("error", "") + "\nColor IPR worker retained tracked resources after closure"
                )
        if self._retire in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self._retire)
        hou.putenv("OCIO", self.original_env or "")
        if self.auto_textures:
            if self.original_mode is None:
                hou.unsetenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
            else:
                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", self.original_mode)
        hou.Color.reloadOCIO()
        OCIO.SetCurrentConfig(self.original)
        self.report["result"] = "passed" if self.code == 0 else "failed"
        self.save()
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


test = ColorLifecycle()
