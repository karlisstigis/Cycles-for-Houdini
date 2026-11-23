# SPDX-License-Identifier: Apache-2.0
"""Actual shared native IPR/panel edits on the richer SOP demonstration scene."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from cycles_houdini import registry


class CourtyardLifecycle:
    def __init__(self):
        self.root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.destination = artifacts_root() / "examples/sop-fx-courtyard"
        self.original_env = hou.getenv("OCIO")
        hou.hipFile.load(str(self.destination / "sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True)
        hou.putenv("OCIO", str(self.destination / "showcase.ocio"))
        hou.Color.reloadOCIO()
        self.rop = hou.node("/out/cycles_sop_courtyard")
        self.camera = hou.node("/obj/courtyard_camera")
        self.stone = hou.node("/mat/courtyard_noise_displaced_stone")
        self.hero = hou.node("/obj/fx_courtyard/TWISTED_TURBINE")
        self.device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        set_rop_devices(self.rop, self.device)
        for name, value in dict(resx=640, resy=360, samples=32).items():
            self.rop.parm(name).set(value)
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=self.device, host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0
        self.stage = 0
        self.panel_tab = None
        self.panel = None
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

    def current(self, entry):
        if not entry or entry.error:
            return False
        frame = entry.session.frame_info()
        if not frame or not frame["complete"] or frame["generation"] != entry.session.status()["generation"]:
            return False
        pixels = np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).reshape(360, 640, 4)
        assert np.isfinite(pixels).all() and np.std(pixels[:, :, :3]) > 0.05
        try:
            actual = self.viewer.pixel("C", 320, 180)
        except hou.OperationFailed:
            return False
        self.report["native_sample"] = list(actual)
        self.report["expected_native_sample"] = pixels[180, 320].tolist()
        return np.allclose(actual, pixels[180, 320], rtol=0, atol=0.001)

    def record(self, name, entry):
        assert len(registry.entries()) == 1 and self.panel.entry is entry
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                frame=entry.session.frame_info(),
            )
        )
        self.save()

    def advance(self, stage, entry):
        self.before = entry.session.status()
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
            if time.monotonic() - self.started > 100:
                raise TimeoutError("SOP courtyard interaction stalled")
            if not self.current(entry):
                return
            if self.stage == 0:
                assert entry.helper_pid and entry.session.status()["packed_instances"] == 54
                assert entry.session.status()["points"] == 360 and entry.session.status()["curves"] == 265
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.window().resize(1200, 820)
                self.panel.set_rop(self.rop)
                self.panel.poll()
                assert self.panel.entry is entry
                self.record("Full SOP content in native IPR and panel shares one session", entry)
                label = "cpu" if self.device == "CPU" else "optix"
                self.panel.grab().save(str(self.destination / f"panel-{label}.png"))
                self.advance(1, entry)
                self.stone.parm("rock_relief").set(0.95)
            elif self.stage == 1:
                if entry.session.status()["generation"] <= self.before["generation"]:
                    return
                assert entry.session.status()["displacement_rebuilds"] > self.before["displacement_rebuilds"]
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                self.record("Promoted noise displacement edit updates both viewers without SOP reconversion", entry)
                self.advance(2, entry)
                self.camera.parm("tx").set(self.camera.evalParm("tx") + 0.2)
            elif self.stage == 2:
                if entry.session.status()["generation"] <= self.before["generation"]:
                    return
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                assert entry.session.status()["displacement_rebuilds"] == self.before["displacement_rebuilds"]
                self.record("Camera edit retains geometry and true displacement", entry)
                self.advance(3, entry)
                self.hero.parm("twist").set(3.1)
            elif self.stage == 3:
                if entry.session.status()["generation"] <= self.before["generation"]:
                    return
                assert entry.session.status()["geometry_conversions"] > self.before["geometry_conversions"]
                assert entry.session.status()["packed_instances"] == 54
                self.record("Editable sculpture SOP rebuild reaches both viewers", entry)
                self.panel.poll()
                generation = entry.session.status()["generation"]
                self.panel.pass_menu.setCurrentIndex(self.panel.pass_menu.findData("normal"))
                self.panel.poll()
                self.panel.pass_menu.setCurrentIndex(self.panel.pass_menu.findData("combined"))
                self.panel.exposure.setValue(0.5)
                self.panel.poll()
                self.panel.snapshot()
                assert len(self.panel._snapshots) == 1
                assert entry.session.status()["generation"] == generation
                self.record("Pass, exposure and snapshot controls preserve tracing generation", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self._tick)
        self.viewer.killRender()
        self.viewer.close()
        entry = registry.get(self.key)
        if self.panel:
            self.panel.dispose()
            self.panel_tab.close()
        registry.close_all()
        if entry:
            self.report["closed"] = entry.session.status()
            assert (
                self.report["closed"]["device_allocation_bytes"] == self.report["closed"]["owned_geometry_bytes"] == 0
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


test = CourtyardLifecycle()
