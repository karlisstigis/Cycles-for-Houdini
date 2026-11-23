# SPDX-License-Identifier: Apache-2.0
"""One heavy viewer: live spatial displacement, camera dependencies and source repair."""

from support.paths import artifacts_root

import hashlib, json, os, re, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties
from spatial_displacement_fixture import Fixture
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow


class Workflow(ParameterWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_SPATIAL_VIEWER") == "native"
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.references = (
            artifacts_root() / "stage2/displacement/spatial" / ("references-" + re.sub("[^A-Za-z0-9_-]", "_", device))
        )
        self.specs = {spec["name"]: spec for spec in json.loads((self.references / "specifications.json").read_text())}
        self.images = {}
        self.f = Fixture(device)
        self.configure("world_object_vector")
        heavy = hou.node("/obj").createNode("geo", "resident_spatial_instances", run_init_scripts=False)
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
        self.cook_sources = (self.f.sop, self.f.output, torus)
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
        self.settings.setCurrentNode(self.f.sop)
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
        self.expected = "world_object_vector"
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

    def configure(self, name):
        self.f.configure(self.specs[name])
        self.f.grid.setParms(dict(rows=129, cols=257))

    def image(self, name):
        if name not in self.images:
            path = self.references / (name + "-3-c.exr")
            image = oiio.ImageBuf(str(path))
            channels = list(image.spec().channelnames)
            self.images[name] = (
                np.flipud(image.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]]),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return self.images[name]

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
                assert abs(float(value) - float(self.text)) < 1e-6 or now < self.commit_deadline, (
                    self.parm.path(),
                    value,
                    self.text,
                )
                if abs(float(value) - float(self.text)) >= 1e-6:
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
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
            assert (
                np.isfinite(pixels).all()
                and status["geometry_resources"] == 2
                and status["object_resources"] == 13
                and status["packed_instances"] == 12
            ), status
            expected, digest = self.image(self.expected)
            difference = float(np.abs(pixels - expected).mean())
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
                if not interim or any(interim[-1][key] != observed[key] for key in ("phase", "generation", "sequence")):
                    interim.append(observed)
                if now < self.image_deadline:
                    return
                np.savez(self.path.parent / "failed-image.npz", expected=expected, actual=pixels)
                raise AssertionError((self.phase, difference))
            assert not status["motion_warnings"], status
            codes = {issue["code"] for issue in status["source_diagnostics"]}
            assert codes == ({"material.unsupported_attribute"} if self.phase == "source_broken" else set()), status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 96):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((35, 48), (55, 48), (80, 48), (128, 48), (150, 48))
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
            if self.phase in (
                "camera",
                "camera_restored",
                "camera_space_shift",
                "camera_space_restored",
                "object_slow",
                "object_restored",
            ):
                assert all(status[key] == self.retained[key] for key in ("geometry_conversions", "extractions")), status
                counts = {node.path(): node.cookCount() for node in self.cook_sources}
                assert counts == self.retained_cooks, (counts, self.retained_cooks)
                if self.phase.startswith("camera_space") or self.phase.startswith("object"):
                    assert status["displacement_rebuilds"] > self.retained["displacement_rebuilds"], status
                else:
                    assert status["displacement_rebuilds"] == self.retained["displacement_rebuilds"], status
                self.report.setdefault("navigation_cooks", []).append(dict(phase=self.phase, counts=counts))
            if self.phase in ("source_broken", "source_repaired"):
                assert all(
                    status[key] == self.retained[key]
                    for key in ("geometry_conversions", "extractions", "displacement_rebuilds")
                ), status
            self.record(self.phase, status)
            self.report["events"][-1].update(mean_difference=difference, reference_sha256=digest)
            self.retained = status
            self.retained_cooks = {node.path(): node.cookCount() for node in self.cook_sources}
            if self.phase == "first":
                self.edit(self.f.materials[0]["transform"].parmTuple("vector")[0], 0.4, "input_high", "gui_input_high")
                return
            if self.phase == "input_high":
                self.edit(
                    self.f.materials[0]["transform"].parmTuple("vector")[0],
                    0.25,
                    "input_restored",
                    "world_object_vector",
                )
                return
            if self.phase == "input_restored":
                self.edit(self.f.obj.parm("object_rate"), 9, "object_slow", "gui_object_slow")
                return
            if self.phase == "object_slow":
                self.edit(self.f.obj.parm("object_rate"), 18, "object_restored", "world_object_vector")
                return
            if self.phase == "object_restored":
                self.edit(self.f.camera.parm("tx"), 0.1, "camera", "gui_plain_camera_shift")
                return
            if self.phase == "camera":
                self.edit(self.f.camera.parm("tx"), 0, "camera_restored", "world_object_vector")
                return
            if self.phase == "camera_restored":
                self.configure("gui_camera_point")
                self.phase = "camera_space_ready"
                self.expected = "gui_camera_point"
                return
            if self.phase == "camera_space_ready":
                self.edit(self.f.camera.parm("tx"), 0.2, "camera_space_shift", "gui_camera_shift")
                return
            if self.phase == "camera_space_shift":
                self.edit(self.f.camera.parm("tx"), 0.1, "camera_space_restored", "gui_camera_point")
                return
            if self.phase == "camera_space_restored":
                self.configure("generated_scalar")
                self.phase = "generated_ready"
                self.expected = "generated_scalar"
                return
            if self.phase == "generated_ready":
                self.edit(self.f.sop.parm("shape_rate"), 24, "generated_slow", "gui_generated_slow")
                return
            if self.phase == "generated_slow":
                self.edit(self.f.sop.parm("shape_rate"), 48, "generated_restored", "generated_scalar")
                return
            if self.phase == "generated_restored":
                self.configure("world_object_vector")
                nodes = self.f.materials[0]
                nodes["surface"].setNamedInput("color", nodes["color"], "vector")
                self.phase = "surface_generated"
                self.expected = "gui_surface_generated"
                return
            if self.phase == "surface_generated":
                self.f.materials[0]["coordinate"].parm("from_dupli").set(1)
                self.phase = "source_broken"
                self.expected = "gui_surface_diagnostic"
                return
            if self.phase == "source_broken":
                self.f.materials[0]["coordinate"].parm("from_dupli").set(0)
                self.phase = "source_repaired"
                self.expected = "gui_surface_generated"
                return
            if self.phase == "source_repaired":
                target = self.path.parent / "spatial-displacement-viewer.png"
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
