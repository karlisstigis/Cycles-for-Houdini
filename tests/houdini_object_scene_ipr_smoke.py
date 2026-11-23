# SPDX-License-Identifier: Apache-2.0
"""Real shared IPR/panel OBJ creation, flags, masks, lights and failure recovery."""

import json
import os
from pathlib import Path
import sys
import time
import traceback
import hou
import numpy as np
from cycles_houdini import registry

sys.path.insert(0, str(Path(__file__).parent))
from native_object_fixture import fixture


class ObjectLifecycle:
    def __init__(self):
        self.device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.f = fixture(self.device)
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.report = dict(device=self.device, host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0
        self.stage = 0
        self.panel = None
        self.panel_tab = None
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.f.rop)
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
        if not entry or entry.error or not entry.helper_pid:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if not frame or not frame["complete"] or frame["generation"] != status["generation"]:
            return False
        if self.stage and status["generation"] <= self.before["generation"]:
            return False
        self.image = np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).reshape(
            96, 128, 4
        )
        assert np.isfinite(self.image).all()
        try:
            actual = self.viewer.pixel("C", 96, 48)
        except hou.OperationFailed:
            return False
        return np.allclose(actual, self.image[48, 96], rtol=0, atol=0.001)

    def expect(self, x, color, y=0, alpha=1):
        x -= self.f.camera.evalParm("tx")
        ix, iy = round(64 + 32 * x), round(48 + 32 * y)
        actual = self.image[iy - 1 : iy + 2, ix - 1 : ix + 2].mean((0, 1))
        assert np.allclose(actual, (*color, alpha), atol=0.001, rtol=0), (self.stage, actual, color)

    def record(self, name, entry):
        assert len(registry.entries()) == 1 and self.panel.entry is entry
        self.report["events"].append(
            dict(
                name=name, seconds=time.monotonic() - self.started, status=entry.session.status(), soho=entry.last_soho
            )
        )
        self.save()

    def advance(self, stage, entry):
        self.before = entry.session.status()
        self.stage = stage

    def tick(self):
        if time.monotonic() - self.last_tick < 0.08:
            return
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage, status=entry.session.status() if entry else None, error=entry.error if entry else ""
            )
            self.save()
            if time.monotonic() - self.started > 100:
                raise TimeoutError("Native OBJ scene IPR update stalled")
            if not self.current(entry):
                return
            f = self.f
            if self.stage == 0:
                self.expect(-1, f.colors["red"])
                self.expect(1, f.colors["blue"])
                self.expect(0, (0, 0, 0), 1, 0)
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(f.rop)
                self.panel.poll()
                assert entry.helper_pid and entry.session.status()["light_resources"] == 1
                self.record("Native OBJ scene renders through IPR and shared panel", entry)
                self.advance(1, entry)
                f.hidden.setDisplayFlag(True)
            elif self.stage == 1:
                self.expect(0, f.colors["green"], 1)
                assert entry.session.status()["object_resources"] == 3
                self.record("Hidden object display flag adds it automatically", entry)
                self.advance(2, entry)
                f.parent.parm("tx").set(1.2)
            elif self.stage == 2:
                self.expect(-0.8, f.colors["red"])
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                self.record("Parent subnet transform updates without SOP conversion", entry)
                self.advance(3, entry)
                f.camera.parm("tx").set(0.1)
            elif self.stage == 3:
                self.expect(-0.8, f.colors["red"])
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                self.record("Camera override retains all OBJ resources", entry)
                self.advance(4, entry)
                f.light.parm("light_exposure").set(1)
            elif self.stage == 4:
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                assert entry.last_values["lights"][0]["strength"] == (200.0, 200.0, 200.0)
                self.record("Native hlight exposure propagates to both viewers", entry)
                self.advance(5, entry)
                f.rop.parm("excludeobject").set(f.blue.path())
            elif self.stage == 5:
                self.expect(1, (0, 0, 0), alpha=0)
                assert entry.session.status()["object_resources"] == 2
                self.record("Exclusion removes its geometry and object", entry)
                self.advance(6, entry)
                f.rop.parm("excludeobject").set("")
                f.hidden.setDisplayFlag(False)
                f.rop.parm("forceobject").set(f.hidden.path())
            elif self.stage == 6:
                self.expect(0, f.colors["green"], 1)
                self.expect(1, f.colors["blue"])
                self.record("Force Objects survives a hidden display flag in native IPR", entry)
                self.bundle = hou.addNodeBundle("ipr_native_selection")
                self.bundle.addNode(f.red)
                self.advance(7, entry)
                f.rop.parm("vobject").set("@" + self.bundle.name())
                f.rop.parm("forceobject").set("")
            elif self.stage == 7:
                self.expect(-0.8, f.colors["red"])
                self.expect(1, (0, 0, 0), alpha=0)
                self.record("Bundle-based scene selection reaches native IPR", entry)
                self.advance(8, entry)
                self.bundle.addNode(f.blue)
            elif self.stage == 8:
                self.expect(1, f.colors["blue"])
                self.record("Bundle membership change wakes an automatic render", entry)
                self.advance(9, entry)
                f.materials["blue"].node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
            elif self.stage == 9:
                self.expect(1, (0.1, 0.7, 0.2))
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                self.record("Discovered OBJ material edit retains geometry", entry)
                self.advance(10, entry)
                f.light.parm("coneenable").set(1)
            elif self.stage == 10:
                self.expect(1, (0.1, 0.7, 0.2))
                assert entry.session.status()["geometry_conversions"] == self.before["geometry_conversions"]
                assert entry.session.status()["light_resources"] == 1 and not entry.error
                self.record("Supported native cone falloff updates without recopying OBJ geometry", entry)
                self.advance(11, entry)
                f.light.parm("coneenable").set(0)
            elif self.stage == 11:
                self.expect(1, (0.1, 0.7, 0.2))
                self.record("Disabling native cone falloff retains the OBJ scene", entry)
                self.advance(12, entry)
                f.blue.destroy()
            elif self.stage == 12:
                self.expect(1, (0, 0, 0), alpha=0)
                assert entry.session.status()["object_resources"] == 1
                self.record("OBJ deletion retires the discovered component", entry)
                self.advance(13, entry)
                new = f.scene.createNode("geo", "new_receiver", run_init_scripts=False)
                source = new.createNode("python", "surface")
                source.parm("python").set(f.code)
                source.setRenderFlag(True)
                new.parm("tx").set(1)
                new.parm("shop_materialpath").set(f.materials["green"].path())
                self.bundle.addNode(new)
            elif self.stage == 13:
                self.expect(1, f.colors["green"])
                assert entry.session.status()["object_resources"] == 2
                self.record("New OBJ/SOP and bundle membership discover resources automatically", entry)
                self.panel.poll()
                self.panel.grab().save(str(Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).with_suffix(".png")))
                self.advance(14, entry)
                f.rop.parm("vobject").set("")
                f.rop.parm("lights").set("")
            elif self.stage == 14:
                assert (
                    entry.session.status()["geometry_resources"]
                    == entry.session.status()["object_resources"]
                    == entry.session.status()["light_resources"]
                    == 0
                )
                self.record("Empty scene selection retires resources in both viewers", entry)
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
                self.report["closed"]["device_allocation_bytes"]
                == self.report["closed"]["owned_geometry_bytes"]
                == self.report["closed"]["partition_cache_bytes"]
                == 0
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
            hou.ui.postEventCallback(self.exit)

    def exit(self):
        try:
            hou.exit(self.code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = ObjectLifecycle()
