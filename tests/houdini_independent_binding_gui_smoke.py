# SPDX-License-Identifier: Apache-2.0
"""One viewer: independent Material SOP motion, repair and navigation on heavy geometry."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry, object_properties
from sampled_override_fixture import Fixture
from independent_binding_fixture import motion_pixels
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow


class Workflow(ParameterWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_BINDING_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.setup("parameter")
        self.f.rop.parm("geometry_motion").set("velocity")
        self.f.obj.node("grid1").setParms(dict(rows=129, cols=257))
        self.f.assigned.parm("test_speed").set(24)
        self.f.sop.addSpareParmTuple(hou.FloatParmTemplate("test_velocity", "Velocity", 1, default_value=(48,)))
        self.f.sop.parm("snippet").set('v@N=set(1,0,0);v@v=set(chf("test_velocity"),0,0);@P.x+=@Time*900;')
        self.f.output.addSpareParmTuple(
            hou.IntParmTemplate("test_missing", "Test Missing Shutter Binding", 1, default_value=(0,))
        )
        self.f.output.parm("snippet").set(
            'if(@Time>0 && chi("test_missing"))s@shop_materialpath="/mat/gui_missing_shutter_binding";'
        )
        heavy = hou.node("/obj").createNode("geo", "resident_shared_geometry", run_init_scripts=False)
        object_properties.install(heavy)
        heavy.parm("cycles_geometry_motion").set("velocity")
        torus = heavy.createNode("torus", "million_triangle_prototype")
        torus.setParms(dict(type=0, rows=512, cols=1024))
        self.cook_sources = (self.f.sop, self.f.assigned, self.f.output, torus)
        targets = heavy.createNode("grid", "twelve_targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((18, 0, -8))
        copies = heavy.createNode("copytopoints", "shared_instances")
        copies.setInput(0, torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        self.f.rop.parm("vobject").set(self.f.obj.path() + " " + heavy.path())
        self.camera = hou.node(self.f.rop.evalParm("camera"))
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            scope=__doc__,
            physical_mouse_certification=False,
            events=[],
            tick_intervals=[],
            render_clicks=1,
        )
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 550))
        self.settings.setCurrentNode(self.f.assigned)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.expected = 72
        self.shift = 0
        self.began = self.last_tick = time.monotonic()
        self.next_at = self.saved_at = 0
        self.generation = 0
        self.finished = self.busy = False
        self.commit_deadline = None
        self.image_phase = None
        self.image_deadline = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def finish(self, error=None):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        super().finish(error)

    def tick(self):
        if self.phase in ("mount", "focus", "keys", "commit"):
            return super().tick()
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["tick_intervals"].append(dict(phase=self.phase, seconds=now - self.last_tick))
            self.last_tick = now
            assert now - self.began < 300, self.phase
            if now - self.saved_at > 0.5:
                self.report["phase"] = self.phase
                self.path.write_text(json.dumps(self.report), encoding="utf-8")
                self.saved_at = now
            if now < self.next_at:
                return
            if self.commit_deadline:
                value = self.parm.eval()
                assert float(value) == float(self.text) or now < self.commit_deadline, (
                    self.parm.path(),
                    value,
                    self.text,
                )
                if float(value) != float(self.text):
                    return
                self.commit_deadline = None
            entry = registry.get(f"rop:{self.f.rop.sessionId()}")
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] <= self.generation
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            frame = entry.session.frame()
            pixels = self.f.pixels(frame)["combined"][:, :, :3]
            assert (
                np.isfinite(pixels).all()
                and status["geometry_resources"] == 2
                and status["object_resources"] == 13
                and status["packed_instances"] == 12
            ), status
            if self.image_phase != self.phase:
                self.image_phase = self.phase
                self.image_deadline = now + 8
            half = self.expected / 96
            expected = motion_pixels((-half, 0, half), self.shift)
            difference = float(np.abs(pixels - expected).mean())
            if difference >= 0.0008:
                observed = dict(
                    phase=self.phase,
                    generation=info["generation"],
                    sequence=info["sequence"],
                    mean_difference=difference,
                )
                interim = self.report.setdefault("interim_frames", [])
                if not interim or any(interim[-1][key] != observed[key] for key in ("phase", "generation", "sequence")):
                    interim.append(observed)
                if now < self.image_deadline:
                    return
                np.savez(self.path.parent / "failed-image.npz", expected=expected, actual=pixels)
                raise AssertionError((self.phase, difference))
            assert {issue["code"] for issue in status["source_diagnostics"]} == (
                {"motion.displacement_binding"} if self.phase == "missing" else set()
            ), status
            assert not status["motion_warnings"], status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 90):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((35, 45), (55, 45), (80, 45), (128, 45), (150, 45))
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1
            if self.report["events"]:
                assert status["renderer_pid"] == self.pid, status
            else:
                self.pid = status["renderer_pid"]
            if self.phase.startswith("camera") or self.phase.startswith("object"):
                assert all(
                    status[key] == self.retained[key]
                    for key in ("geometry_conversions", "extractions", "displacement_rebuilds")
                ), status
                # A camera-only fast submission intentionally keeps the last
                # geometry-capture statistics. Check the actual host sources,
                # rather than treating those latched values as new cook work.
                counts = {node.path(): node.cookCount() for node in self.cook_sources}
                assert counts == self.retained_cooks, (self.phase, counts, self.retained_cooks)
                self.report.setdefault("navigation_cooks", []).append(dict(phase=self.phase, counts=counts))
            self.record(self.phase, status)
            if self.phase == "first":
                self.edit(self.f.assigned.parm("test_speed"), 0, "override_static", 48)
                return
            if self.phase == "override_static":
                self.edit(self.f.assigned.parm("test_speed"), 24, "override_repaired", 72)
                return
            if self.phase == "override_repaired":
                self.edit(self.f.output.parm("test_missing"), 1, "missing", 48)
                return
            if self.phase == "missing":
                self.edit(self.f.output.parm("test_missing"), 0, "binding_repaired", 72)
                return
            if self.phase == "binding_repaired":
                self.edit(self.f.sop.parm("test_velocity"), 0, "velocity_static", 24)
                return
            if self.phase == "velocity_static":
                self.edit(self.f.sop.parm("test_velocity"), 48, "velocity_repaired", 72)
                return
            if self.phase == "velocity_repaired":
                self.retained = status
                self.retained_cooks = {node.path(): node.cookCount() for node in self.cook_sources}
                self.shift = -2
                self.edit(self.camera.parm("tx"), 0.05, "camera", 72)
                return
            if self.phase == "camera":
                self.shift = 0
                self.edit(self.camera.parm("tx"), 0, "camera_repaired", 72)
                return
            if self.phase == "camera_repaired":
                self.shift = 2
                self.edit(self.f.obj.parm("tx"), -0.45, "object", 72)
                return
            if self.phase == "object":
                self.shift = 0
                self.edit(self.f.obj.parm("tx"), -0.5, "object_repaired", 72)
                return
            if self.phase == "object_repaired":
                target = self.path.parent / "independent-binding-viewer.png"
                self.report["viewer_capture"] = str(target)
                self.report["viewer_capture_saved"] = (
                    hou.qt.floatingPanelWindow(self.tab.floatingPanel()).grab().save(str(target))
                )
                if self.native:
                    self.tab.killRender()
                else:
                    QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False


if __name__ == "__main__":
    workflow = Workflow()
