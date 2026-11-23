# SPDX-License-Identifier: Apache-2.0
"""One heavy viewer: native exposure edits, independent images and local repair."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties
from camera_sampling_fixture import Fixture, reference
from houdini_camera_lens_motion_gui_smoke import Workflow as CameraWorkflow
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow


class Workflow(CameraWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_CAMERA_SAMPLING_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.grid.setParms(dict(rows=129, cols=257))
        heavy = hou.node("/obj").createNode("geo", "resident_exposure_instances", run_init_scripts=False)
        object_properties.install(heavy)
        heavy.parm("cycles_geometry_motion").set("velocity")
        torus = heavy.createNode("torus", "million_triangle_prototype")
        torus.setParms(dict(type=0, rows=512, cols=1024))
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
        self.cook_sources = (self.f.sop, torus)
        self.retained = None
        self.retained_cooks = {}
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
        self.settings.setCurrentNode(self.f.camera)
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
        self.expected = ("uniform", False, 0.1, 0.0)
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

    def tick(self):
        if self.phase in ("mount", "focus", "keys", "commit"):
            return ParameterWorkflow.tick(self)
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
                matches = abs(float(value) - float(self.text)) < 1e-6
                assert matches or now < self.commit_deadline, (self.parm.path(), value, self.text)
                if not matches:
                    return
                self.commit_deadline = None
            entry = registry.get(f"rop:{self.f.rop.sessionId()}")
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            retained = self.phase == "unused_bad_curve" and entry.applied_serial == entry.change_serial
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] < self.generation
                or (status["generation"] == self.generation and not retained)
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if retained:
                assert min(self.f.camera.parm("cycles_shutter_curve").eval().values()) < 0
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            plane = entry.session.frame()["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
            assert (
                status["geometry_resources"] == 2
                and status["object_resources"] == 13
                and status["packed_instances"] == 12
            ), status
            shape, rolling, duration, camera_x = self.expected
            expected = reference(shape, rolling, duration, camera_x=camera_x)
            difference = float(np.abs(pixels - expected).mean())
            assert np.isfinite(pixels).all()
            if self.image_phase != self.phase:
                self.image_phase = self.phase
                self.image_deadline = now + 8
            if difference >= 0.0012:
                observed = dict(
                    phase=self.phase,
                    generation=info["generation"],
                    sequence=info["sequence"],
                    mean_difference=difference,
                )
                interim = self.report.setdefault("interim_frames", [])
                if not interim or any(interim[-1][k] != observed[k] for k in ("phase", "generation", "sequence")):
                    interim.append(observed)
                if now < self.image_deadline:
                    return
                np.savez(self.path.parent / "failed-image.npz", expected=expected, actual=pixels)
                raise AssertionError((self.phase, difference))
            codes = {
                "active_bad_curve": {"camera.invalid_shutter_curve"},
                "scan_invalid": {"camera.invalid_rolling_shutter"},
                "scan_animated": {"camera.shutter_rolling"},
                "ramp_animated": {"camera.shutter_curve"},
            }.get(self.phase, set())
            assert {d["code"] for d in status["source_diagnostics"]} == codes and not status["motion_warnings"], status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 96):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((75, 12), (80, 12), (80, 48), (85, 48), (80, 84))
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1
            counts = {node.path(): node.cookCount() for node in self.cook_sources}
            if self.retained:
                assert status["renderer_pid"] == self.pid and counts == self.retained_cooks, (
                    counts,
                    self.retained_cooks,
                )
                assert all(
                    status[k] == self.retained[k]
                    for k in ("geometry_conversions", "extractions", "subdivision_tessellations")
                ), status
                if status["render_revision"] != self.retained["render_revision"]:
                    assert not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys()
                self.report.setdefault("navigation_cooks", []).append(dict(phase=self.phase, counts=counts))
            else:
                self.pid = status["renderer_pid"]
            self.record(self.phase, status)
            self.report["events"][-1].update(mean_difference=difference, expected_exposure=self.expected)
            self.retained = status
            self.retained_cooks = counts
            if self.phase == "first":
                self.f.set_rolling(True, 0.25)
                self.phase = "roll_ready"
                self.expected = ("uniform", True, 0.25, 0.0)
                return
            if self.phase == "roll_ready":
                self.edit(
                    self.f.camera.parm("cycles_rolling_duration"), 0.5, "duration_half", ("uniform", True, 0.5, 0.0)
                )
                return
            if self.phase == "duration_half":
                self.edit(self.f.camera.parm("cycles_rolling_duration"), 0, "pure_scan", ("uniform", True, 0.0, 0.0))
                return
            if self.phase == "pure_scan":
                self.f.set_curve("negative")
                self.phase = "unused_bad_curve"
                self.next_at = now + 0.3
                return
            if self.phase == "unused_bad_curve":
                self.edit(
                    self.f.camera.parm("cycles_rolling_duration"),
                    0.25,
                    "active_bad_curve",
                    ("uniform", True, 0.25, 0.0),
                )
                return
            if self.phase == "active_bad_curve":
                self.f.set_curve("triangle")
                self.phase = "curve_repaired"
                self.expected = ("triangle", True, 0.25, 0.0)
                return
            if self.phase == "curve_repaired":
                self.f.camera.parm("cycles_rolling_duration").setExpression("float('nan')", hou.exprLanguage.Python)
                self.phase = "scan_invalid"
                self.expected = ("triangle", False, 0.1, 0.0)
                return
            if self.phase == "scan_invalid":
                self.edit(
                    self.f.camera.parm("cycles_rolling_duration"), 0.25, "scan_repaired", ("triangle", True, 0.25, 0.0)
                )
                return
            if self.phase == "scan_repaired":
                self.f.camera.parm("cycles_rolling_duration").setExpression(".25+$T*24", hou.exprLanguage.Hscript)
                self.phase = "scan_animated"
                return
            if self.phase == "scan_animated":
                self.edit(
                    self.f.camera.parm("cycles_rolling_duration"), 0.5, "scan_constant", ("triangle", True, 0.5, 0.0)
                )
                return
            if self.phase == "scan_constant":
                self.f.set_curve("uniform")
                self.f.camera.parm("cycles_use_shutter_curve").set(1)
                self.f.camera.parm("cycles_shutter_curve1value").setExpression("1+$T*24", hou.exprLanguage.Hscript)
                self.phase = "ramp_animated"
                self.expected = ("uniform", True, 0.5, 0.0)
                return
            if self.phase == "ramp_animated":
                self.f.camera.parm("cycles_shutter_curve1value").deleteAllKeyframes()
                self.f.set_curve("uniform")
                self.phase = "ramp_repaired"
                return
            if self.phase == "ramp_repaired":
                self.f.set_curve("triangle")
                self.phase = "shaped_again"
                self.expected = ("triangle", True, 0.5, 0.0)
                return
            if self.phase == "shaped_again":
                self.edit(self.f.camera.parm("tx"), 0.05, "camera_shift", ("triangle", True, 0.5, 0.05))
                return
            if self.phase == "camera_shift":
                self.edit(self.f.camera.parm("tx"), 0, "camera_restored", ("triangle", True, 0.5, 0.0))
                return
            if self.phase == "camera_restored":
                self.edit(
                    self.f.camera.parm("cycles_rolling_duration"), 1, "global_shutter", ("triangle", True, 1.0, 0.0)
                )
                return
            if self.phase == "global_shutter":
                target = self.path.parent / "camera-sampling-viewer.png"
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
