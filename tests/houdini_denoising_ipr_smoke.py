# SPDX-License-Identifier: Apache-2.0
"""One native IPR viewer: progressive denoising, schema changes and restart."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from gui_shutdown import retire_and_exit


class Test:
    def __init__(self):
        self.destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[], device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.started = time.monotonic()
        self.stage = 0
        self.in_tick = False
        self.finished = False
        self.sessions = []
        self.settings_pane = None
        obj = hou.node("/obj").createNode("geo", "denoise_quad", run_init_scripts=False)
        source = obj.createNode("grid")
        source.setParms(dict(orient=0, rows=2, cols=2))
        source.parmTuple("size").set((2, 2))
        source.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "denoise_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "denoise_rop")
        set_rop_devices(self.rop, self.report["device"])
        self.rop.setParms(
            dict(
                source_mode="sop",
                soppath=source.path(),
                camera=camera.path(),
                resx=96,
                resy=64,
                viewer_interactive=1,
                ipr_samples=1048576,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                material_kind="emission",
                emission_strength=1,
                use_material_assignments=0,
            )
        )
        self.rop.parmTuple("basecolor").set((0.4, 0.2, 0.08))
        self.rop.parmTuple("background").set((0, 0, 0))
        self.key = f"rop:{self.rop.sessionId()}"
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        hou.ui.addEventLoopCallback(self.tick)
        self.viewer.startRender()

    def visible(self, entry, plane="C"):
        frame = entry.session.frame_info()
        if not frame or frame["generation"] != entry.session.status()["generation"]:
            return False
        try:
            pixel = self.viewer.pixel(plane, 48, 32)
        except hou.OperationFailed:
            return False
        return np.allclose(pixel[:3], (0.4, 0.2, 0.08), atol=0.001)

    def record(self, name, entry):
        self.report["events"].append(dict(name=name, status=entry.session.status(), frame=entry.session.frame_info()))

    def denoised_visible(self, entry):
        frame = entry.session.frame()
        if not frame or "noisy_combined" not in frame["planes"]:
            return False
        expected = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)[32, 48, :3]
        if not np.allclose(expected, (0.4, 0.2, 0.08), atol=0.025, rtol=0.05):
            return False
        try:
            actual = self.viewer.pixel("C", 48, 32)
        except hou.OperationFailed:
            return False
        if not np.allclose(actual[:3], expected, atol=0.001):
            return False
        self.report["denoised_pixel"] = expected.tolist()
        self.report["native_denoised_pixel"] = list(actual)
        return True

    def tick(self):
        if self.in_tick:
            return
        self.in_tick = True
        try:
            if time.monotonic() - self.started > 100:
                raise TimeoutError("Denoising IPR workflow stalled")
            if self.stage == 4:
                # With one viewer, killRender also releases the last native
                # owner. A restart can legitimately create a new generation 1.
                if time.monotonic() - self.stopped < 0.3:
                    return
                if self.stopped_session.status()["state"] not in ("cancelled", "closed"):
                    return
                self.viewer.startRender()
                self.stage = 5
                return
            entry = registry.get(self.key)
            if not entry:
                return
            if entry.session not in self.sessions:
                self.sessions.append(entry.session)
            if entry.error:
                raise AssertionError(entry.error)
            if self.stage == 0 and self.visible(entry):
                assert len(registry.entries()) == 1
                self.record("One native IPR viewer owns the live render", entry)
                self.settings_pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
                self.settings_pane.setCurrentNode(self.rop)
                self.stage = 1
            elif self.stage == 1 and self.settings_pane is not None:
                self.before = entry.session.status()
                self.rop.setParms(dict(ipr_use_denoise=1, ipr_denoise_start_sample=0))
                self.stage = 2
            elif (
                self.stage == 2
                and entry.session.status()["generation"] > self.before["generation"]
                and self.visible(entry, "noisy_combined")
                and self.denoised_visible(entry)
            ):
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                info = entry.session.frame_info()
                assert {"combined", "noisy_combined"} <= set(info["passes"])
                assert not info["complete"], "Expected progressive pass delivery during tracing"
                self.record(
                    "Enabled denoising in native ROP; native noisy and denoised beauty update progressively", entry
                )
                self.before = entry.session.status()
                self.rop.parm("ipr_use_denoise").set(0)
                self.stage = 3
            elif (
                self.stage == 3
                and entry.session.status()["generation"] > self.before["generation"]
                and self.visible(entry)
            ):
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                assert entry.session.status()["denoiser"] == "none"
                self.record("Disabled denoising live without recopying geometry", entry)
                self.stopped_session = entry.session
                self.before = entry.session.status()["generation"]
                self.viewer.killRender()
                self.stage = 4
                self.stopped = time.monotonic()
            elif (
                self.stage == 5
                and (entry.session is not self.stopped_session or entry.session.status()["generation"] > self.before)
                and entry.session.status()["state"] in ("rendering", "complete")
                and self.visible(entry)
            ):
                self.record("Stopped and restarted the native render after pass-schema changes", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)
        finally:
            self.in_tick = False

    def finish(self, code):
        if self.finished:
            return
        self.finished = True
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        entry = registry.get(self.key)
        self.viewer.killRender()
        self.viewer.close()
        if self.settings_pane is not None:
            self.settings_pane.close()
        retire_and_exit(self.destination, self.report, self.sessions, code, (registry.close_all,))


test = Test()
