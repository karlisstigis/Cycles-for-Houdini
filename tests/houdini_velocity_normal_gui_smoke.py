# SPDX-License-Identifier: Apache-2.0
"""One viewer: native velocity/normal edits with a million-triangle shared prototype."""

from native_test_runtime import runtime_executable
import json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties
from velocity_normal_fixture import Fixture
from houdini_material_override_gui_smoke import Workflow as ParameterWorkflow


class Workflow(ParameterWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_VELOCITY_NORMAL_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.setup(vertex=True)
        self.f.obj.node("grid1").setParms(dict(rows=129, cols=257))
        self.f.vertex.addSpareParmTuple(
            hou.FloatParmTemplate("test_normal_scale", "Normal Scale", 1, default_value=(1,))
        )
        self.f.vertex.parm("snippet").set('v@N=set(.6,0,.8)*chf("test_normal_scale");')
        self.f.sop.addSpareParmTuple(hou.FloatParmTemplate("test_speed", "Velocity", 1, default_value=(72,)))
        self.f.sop.parm("snippet").set('v@N=set(.6,0,.8);v@v=set(chf("test_speed"),0,0);')
        heavy = hou.node("/obj").createNode("geo", "resident_shared_geometry", run_init_scripts=False)
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
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        reference_dir = self.path.parent / "references"
        run = subprocess.run(
            [
                str(runtime_executable("ch_harness.exe")),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                os.environ["CYCLES_HOUDINI_GUI_DEVICE"],
                "--displacement-motion-reference",
                "--output",
                str(reference_dir),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, (run.stdout, run.stderr)
        image = oiio.ImageBuf(str(reference_dir / "position-3.exr"))
        pixels = image.get_pixels()
        self.reference = np.flipud(pixels[:, :, [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]])
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
        self.settings.setCurrentNode(self.f.vertex)
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
        self.expected = "reference"
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
                np.isfinite(pixels).all() and status["geometry_resources"] == 2 and status["packed_instances"] == 12
            ), status
            if self.image_phase != self.phase:
                self.image_phase = self.phase
                self.image_deadline = now + 8
            if self.expected == "static":
                np.testing.assert_allclose(
                    pixels[30:60, 55:105], np.broadcast_to((0.2, 0.4, 0.8), (30, 50, 3)), atol=2e-5, rtol=0
                )
            else:
                expected = np.roll(self.reference, self.shift, axis=1).copy()
                if self.shift < 0:
                    expected[:, self.shift :] = 0
                elif self.shift > 0:
                    expected[:, : self.shift] = 0
                difference = float(np.abs(pixels - expected).mean())
                if difference >= 0.0008:
                    observed = dict(
                        phase=self.phase,
                        generation=info["generation"],
                        sequence=info["sequence"],
                        mean_difference=difference,
                    )
                    interim = self.report.setdefault("interim_frames", [])
                    if not interim or any(
                        interim[-1][key] != observed[key] for key in ("phase", "generation", "sequence")
                    ):
                        interim.append(observed)
                    # Native SOHO can still publish an accepted earlier edit
                    # before a later control message arrives. Require bounded
                    # convergence to the actual intended image, not an interim
                    # generation; never restart or refresh to make it pass.
                    if now < self.image_deadline:
                        return
                    planes = self.f.pixels(frame)
                    np.savez(self.path.parent / "failed-image.npz", expected=expected, **planes)
                    geometry = self.f.vertex.geometry()
                    self.report["image_failure"] = dict(
                        phase=self.phase,
                        difference=difference,
                        source_normals=geometry.vertexFloatAttribValues("N")[:3],
                        source_velocity=geometry.pointFloatAttribValues("v")[:3],
                        object_x=self.f.obj.evalParm("tx"),
                        camera_x=self.f.camera.evalParm("tx"),
                        scalar_heights=[self.f.scalar.parm("height").evalAtTime(t) for t in (-1 / 96, 0, 1 / 96)],
                    )
                    raise AssertionError((self.phase, difference))
            assert {issue["code"] for issue in status["source_diagnostics"]} == (
                {"geometry.invalid_normal"} if self.phase == "normal_broken" else set()
            ), status
            assert not status["motion_warnings"] and status["motion_cooks"] == 0, status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 90):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=2e-5, rtol=0)
                        for x, y in ((55, 45), (80, 45), (128, 45), (150, 45), (80, 30))
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
            self.record(self.phase, status)
            if self.phase == "first":
                self.edit(self.f.vertex.parm("test_normal_scale"), 0, "normal_broken", "reference")
                return
            if self.phase == "normal_broken":
                self.edit(self.f.vertex.parm("test_normal_scale"), 1, "normal_repaired", "reference")
                return
            if self.phase == "normal_repaired":
                self.edit(self.f.sop.parm("test_speed"), 0, "static", "static")
                return
            if self.phase == "static":
                self.edit(self.f.sop.parm("test_speed"), 72, "velocity_repaired", "reference")
                return
            if self.phase == "velocity_repaired":
                self.retained = status
                self.shift = -2
                self.edit(self.f.camera.parm("tx"), 0.05, "camera", "reference")
                return
            if self.phase == "camera":
                assert all(status[key] == self.retained[key] for key in ("geometry_conversions", "extractions")), status
                self.f.setup(displaced=True, vertex=True)
                self.phase = "displaced"
                self.shift = 0
                return
            if self.phase == "displaced":
                self.retained = status
                self.shift = -2
                self.edit(self.f.camera.parm("tx"), 0.05, "displaced_camera", "reference")
                return
            if self.phase == "displaced_camera":
                assert all(
                    status[key] == self.retained[key]
                    for key in ("geometry_conversions", "extractions", "displacement_rebuilds")
                ), status
                target = self.path.parent / "velocity-normals-viewer.png"
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
