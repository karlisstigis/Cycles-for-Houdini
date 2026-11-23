# SPDX-License-Identifier: Apache-2.0
"""Native IPR and shared-panel alpha, transparency edits and additive emission."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import registry


class AlphaLifecycle:
    def __init__(self):
        self.original = OCIO.GetCurrentConfig()
        self.original_env = hou.getenv("OCIO")
        self.destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(
            str(artifacts_root() / "stage3/color/transparency/native-alpha.hiplc"), suppress_save_prompt=True
        )
        self.rop = hou.node("/out/native_alpha_cycles")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(16777216)
        self.config = self.profile("ACEScg")
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.panel_tab = None
        self.panel = None
        self.stage = 0
        self.started = time.monotonic()
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

    def profile(self, working):
        config = OCIO.Config.CreateFromStream(self.original.serialize())
        config.setWorkingDir(self.original.getWorkingDir())
        config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
        space = OCIO.ColorSpace(name="Native Alpha Display")
        space.setTransform(
            OCIO.MatrixTransform(matrix=[0.5, 0, 0, 0, 0, 0.75, 0, 0, 0, 0, 0.25, 0, 0, 0, 0, 0.125]),
            OCIO.COLORSPACE_DIR_FROM_REFERENCE,
        )
        config.addColorSpace(space)
        config.addDisplayView("Native Alpha", "Coverage", space.getName(), "")
        config.setActiveDisplays("Native Alpha")
        config.setActiveViews("Coverage")
        config.validate()
        path = self.destination / (working.replace(" ", "_").replace("/", "_") + ".ocio")
        path.write_text(config.serialize(), encoding="utf-8")
        hou.putenv("OCIO", str(path))
        hou.Color.reloadOCIO()
        return OCIO.GetCurrentConfig()

    def save(self):
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def frame(self, entry):
        frame = entry.session.frame()
        if not frame:
            return None, None
        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
        assert np.isfinite(pixels).all()
        return frame, pixels

    def native_matches(self, entry, x, y, require_current=True):
        frame, pixels = self.frame(entry)
        if frame is None or (require_current and frame["generation"] != entry.session.status()["generation"]):
            return False
        pixel = pixels[y, x].copy()
        if frame["working_space"] != self.config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName():
            # The source and target configurations retain the original common
            # interchange. Use its independent processor on the owned source RGB.
            pixel[:3] = (
                self.original.getProcessor(
                    frame["working_space"], self.config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
                )
                .getDefaultCPUProcessor()
                .applyRGB(pixel[:3].tolist())
            )
        try:
            actual = self.viewer.pixel("C", x, y)
        except hou.OperationFailed:
            return False
        self.report["native_sample"] = list(actual)
        self.report["expected_native_sample"] = pixel.tolist()
        return np.allclose(actual, pixel, rtol=0, atol=0.001)

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                frame=entry.session.frame_info(),
            )
        )
        assert len(registry.entries()) == 1 and self.panel.entry is entry
        assert not entry.error, entry.error
        self.save()

    def advance(self, stage, entry):
        self.generation = entry.session.status()["generation"]
        self.stage_at = time.monotonic()
        self.stage = stage

    def tick(self):
        if time.monotonic() - self.last_tick < 0.1:
            return
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage, status=entry.session.status() if entry else None, error=entry.error if entry else ""
            )
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Native transparency lifecycle stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.native_matches(entry, 84, 32):
                    return
                _, pixels = self.frame(entry)
                assert pixels[32, 84, 3] == 0 and pixels[32, 12, 3] == 1
                self.conversions = entry.session.status()["geometry_conversions"]
                self.extractions = entry.session.status()["extractions"]
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                self.record("Both viewers share transparent film and fractional coverage", entry)
                self.panel.channels.setCurrentIndex(self.panel.channels.findData("alpha"))
                self.advance(1, entry)
            elif self.stage == 1:
                self.panel.poll()
                if time.monotonic() - self.stage_at < 0.5:
                    return
                assert self.panel.canvas.image.pixelColor(84, 31).getRgb() == (0, 0, 0, 255)
                assert self.panel.canvas.image.pixelColor(12, 31).getRgb() == (255, 255, 255, 255)
                assert entry.session.status()["generation"] == self.generation
                self.panel.channels.setCurrentIndex(self.panel.channels.findData("rgb"))
                self.panel.poll()
                assert self.panel.canvas.image.pixelColor(84, 31).alpha() == 255
                self.record("RGB and alpha inspection change presentation without restarting tracing", entry)
                self.advance(2, entry)
                self.rop.parm("transparent_background").set(0)
            elif self.stage == 2:
                if entry.session.status()["generation"] <= self.generation or not self.native_matches(entry, 84, 32):
                    return
                _, pixels = self.frame(entry)
                assert pixels[32, 84, 3] == 1
                assert entry.session.status()["geometry_conversions"] == self.conversions
                assert entry.session.status()["extractions"] == self.extractions
                self.record("Film toggle updates native IPR without converting geometry", entry)
                self.advance(3, entry)
                self.rop.parm("transparent_background").set(1)
            elif self.stage == 3:
                if entry.session.status()["generation"] <= self.generation or not self.native_matches(entry, 84, 32):
                    return
                _, pixels = self.frame(entry)
                assert pixels[32, 84, 3] == 0
                self.advance(4, entry)
                self.rop.parm("soppath").set("/obj/native_alpha_volume/stash1")
            elif self.stage == 4:
                if entry.session.status()["generation"] <= self.generation or not self.native_matches(entry, 48, 32):
                    return
                _, pixels = self.frame(entry)
                if min(pixels[32, 48, :3]) <= 0.05:
                    return  # Wait for sampled emission, not a fresh empty buffer.
                assert pixels[32, 48, 3] == 0 and min(pixels[32, 48, :3]) > 0.05
                self.panel.poll()
                if self.panel._settings_dirty or self.panel.canvas.image.pixelColor(48, 31).red() == 0:
                    return
                self.record("Zero-alpha emission remains visible in RGB inspection and native pixel data", entry)
                self.viewer.setAutoUpdate(False)
                self.panel.auto.setChecked(False)
                self.advance(5, entry)
                self.config = self.profile("Linear Rec.709 (sRGB)")
            elif self.stage == 5:
                if not entry.session.status()["color_update_pending"] or not self.native_matches(entry, 48, 32, False):
                    return
                if time.monotonic() - self.stage_at < 1:
                    return
                assert entry.session.status()["generation"] == self.generation
                assert entry.session.frame_info()["working_space"] == "ACEScg"
                self.record("Native retained-frame conversion preserves additive RGB and zero alpha", entry)
                self.panel.grab().save(str(self.destination / "native-alpha-panel.png"))
                self.advance(6, entry)
                self.viewer.setAutoUpdate(True)
                self.panel.auto.setChecked(True)
            elif self.stage == 6:
                if entry.session.status()["generation"] <= self.generation or not self.native_matches(entry, 48, 32):
                    return
                assert entry.session.frame_info()["working_space"] == "Linear Rec.709 (sRGB)"
                self.record("Automatic color update resumes the shared transparent render", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self._tick)
        self.viewer.killRender()
        if self.panel_tab:
            self.panel.dispose()
            self.panel_tab.close()
        entry = registry.get(self.key)
        registry.close_all()
        if entry:
            self.report["closed"] = entry.session.status()
            assert (
                self.report["closed"]["owned_geometry_bytes"] == self.report["closed"]["device_allocation_bytes"] == 0
            )
        if self.original_env is None:
            hou.unsetenv("OCIO")
        else:
            hou.putenv("OCIO", self.original_env)
        hou.Color.reloadOCIO()
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


test = AlphaLifecycle()
