# SPDX-License-Identifier: Apache-2.0
"""One heavy viewer: real lens parameter entry, projection changes and repair."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties
from panorama_fixture import Fixture, reference
from houdini_camera_lens_motion_gui_smoke import Workflow as CameraWorkflow
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow


class Workflow(CameraWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_PANORAMA_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.sops[0].setParms(dict(rows=129, cols=257))
        self.f.rop.parm("allowmotionblur").set(1)
        heavy = hou.node("/obj").createNode("geo", "resident_panorama_instances", run_init_scripts=False)
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
        self.f.rop.parm("vobject").set(" ".join(o.path() for o in self.f.objects) + " " + heavy.path())
        self.cook_sources = (*self.f.sops, torus)
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
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 700))
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
        self.expected = dict(self.f.spec)
        self.began = self.last_tick = time.monotonic()
        self.next_at = self.saved_at = 0
        self.generation = 0
        self.finished = self.busy = False
        self.commit_deadline = None
        self.image_phase = None
        self.image_deadline = 0
        self.images = {}
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def lens(self, model, phase, **settings):
        self.expected = dict(self.f.configure(model, **settings))
        self.phase = phase

    def number(self, name, value, phase, **spec):
        self.edit(self.f.camera.parm(name), value, phase, dict(self.expected, **spec))

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
            no_visual_change = (
                self.phase in ("range_invalid", "range_repaired") and entry.applied_serial == entry.change_serial
            )
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] < self.generation
                or (status["generation"] == self.generation and not no_visual_change)
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            plane = entry.session.frame()["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(64, 128, plane["channels"])[:, :, :3]
            assert (
                status["geometry_resources"] == 7
                and status["object_resources"] == 18
                and status["packed_instances"] == 12
            ), status
            key = json.dumps(self.expected, sort_keys=True)
            if key not in self.images:
                self.images[key] = reference(self.expected)
            expected = self.images[key]
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
            codes = {"camera.shutter_panorama"} if self.phase == "range_invalid" else set()
            if {d["code"] for d in status["source_diagnostics"]} != codes and now < self.image_deadline:
                return
            assert {d["code"] for d in status["source_diagnostics"]} == codes and not status["motion_warnings"], status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (128, 64):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((8, 32), (32, 32), (64, 32), (96, 32), (64, 8))
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
            self.report["events"][-1].update(mean_difference=difference, expected_lens=self.expected)
            self.retained = status
            self.retained_cooks = counts
            if self.phase == "first":
                self.number("ry", 15, "camera_turn", angle=15)
                return
            if self.phase == "camera_turn":
                self.number("cycles_longitude_max", 120, "longitude", longitude_max=120)
                return
            if self.phase == "longitude":
                self.lens("fisheye_equidistant", "equidistant")
                return
            if self.phase == "equidistant":
                self.number("cycles_fisheye_fov", 90, "narrow_fisheye", fov=90)
                return
            if self.phase == "narrow_fisheye":
                self.number("cycles_fisheye_fov", 270, "wide_fisheye", fov=270)
                return
            if self.phase == "wide_fisheye":
                self.lens("fisheye_equisolid", "equisolid")
                return
            if self.phase == "equisolid":
                self.number("cycles_fisheye_lens", 7, "lens_size", lens=7)
                return
            if self.phase == "lens_size":
                self.lens("fisheye_equisolid", "vertical_sensor", fit="vertical")
                return
            if self.phase == "vertical_sensor":
                self.number("cycles_sensor_height", 30, "sensor_height", sensor_height=30)
                return
            if self.phase == "sensor_height":
                self.lens("fisheye_lens_polynomial", "polynomial", coefficients=(0, 0, -0.005, 0, 0))
                return
            if self.phase == "polynomial":
                self.number("cycles_fisheye_k2", -0.003, "calibration", coefficients=(0, 0, -0.003, 0, 0))
                return
            if self.phase == "calibration":
                self.lens("mirrorball", "mirrorball")
                return
            if self.phase == "mirrorball":
                self.lens("equiangular_cubemap_face", "cubemap")
                return
            if self.phase == "cubemap":
                self.lens("central_cylindrical", "cylindrical")
                return
            if self.phase == "cylindrical":
                self.number("cycles_cylindrical_radius", 2, "cylinder_radius", radius=2)
                return
            if self.phase == "cylinder_radius":
                self.lens("equirectangular", "equirectangular_again")
                return
            if self.phase == "equirectangular_again":
                self.f.camera.parm("cycles_longitude_max").setExpression(
                    "float('nan') if abs(hou.frame()-1)>.01 else 180", hou.exprLanguage.Python
                )
                self.phase = "range_invalid"
                return
            if self.phase == "range_invalid":
                self.number("cycles_longitude_max", 180, "range_repaired", longitude_max=180)
                return
            if self.phase == "range_repaired":
                self.number("ry", -15, "camera_restored", angle=-15)
                return
            if self.phase == "camera_restored":
                target = self.path.parent / "panorama-viewer.png"
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
