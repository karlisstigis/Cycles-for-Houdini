# SPDX-License-Identifier: Apache-2.0
"""Camera-export stress with a real viewport, heavy FX, subdivision and one IPR."""

import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry

os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "65536"
os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="camera_subdivision_fixture"
)["HeavyResponse"]


class CameraSubdivision(HeavyResponse):
    def __init__(self):
        super().__init__()
        self.last_painted = None
        self.panel.canvas.framePainted.connect(self.record_preview)

    def record_preview(self, generation):
        try:
            metadata = dict(self.panel.canvas.frame_metadata)
            identity = (generation, metadata.get("sequence"))
            if identity == self.last_painted:
                return
            self.last_painted = identity
            metadata.update(stage=self.stage, camera_edits=self.camera_edits)
            self.report["painted_previews"].append(metadata)
            if metadata.get("rendered_width", 960) < 960 and not self.report.get("coarse_preview"):
                path = self.path.with_suffix(".coarse-preview.png")
                self.panel.grab().save(str(path))
                self.report["coarse_preview"] = str(path)
            if self.stage == "camera-drag" and (
                metadata.get("rendered_width", 960) < 960 or 0 < metadata.get("samples", 0) <= 4
            ):
                if not self.report.get("navigation_preview"):
                    path = self.path.with_suffix(".navigation-preview.png")
                    self.panel.grab().save(str(path))
                    self.report["navigation_preview"] = str(path)
        except Exception:
            self.camera_error = traceback.format_exc()

    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.report["adaptive_fixture"] = runpy.run_path(
            str(root / "examples/sop-fx-courtyard/adaptive_scene.py"), run_name="adaptive_fixture"
        )["upgrade"]()
        self.report["camera_subdivision_events"] = []
        self.report["painted_previews"] = []
        self.camera_edits = 0
        self.camera_error = None
        # This workflow edits the Final profile, including its settle-to-eight
        # samples action. Select that real viewer profile rather than the
        # retired sampling-override parameter.
        self.rop.parm("viewer_interactive").set(0)
        if os.environ.get("CYCLES_HOUDINI_LAYOUT_DENOISE") == "1":
            self.rop.parm("use_denoise").set(1)
            self.report["settings"]["denoising"] = True
        self.drag_window = None

    def event(self, name, entry):
        self.report["camera_subdivision_events"].append(
            dict(
                name=name,
                status=entry.session.status(),
                seconds=time.monotonic() - self.began,
                camera_edits=self.camera_edits,
            )
        )
        self.save()

    def move_camera(self):
        if getattr(self, "finished", False) or self.camera_edits >= 60:
            return
        try:
            if self.camera_edits:
                self.check_camera_export()
            self.camera_edits += 1
            # Public viewport camera export while a real Qt mouse press is held.
            viewport = self.scene.curViewport()
            current = viewport.defaultCamera()
            position = current.translation()
            # View translation uses the viewport's orbit coordinates. Verify
            # the exported world matrix against its public view transform.
            current.setTranslation(
                hou.Vector3((self.viewport_translation[0] + 0.008 * self.camera_edits, position[1], position[2]))
            )
            self.expected_camera_transform = viewport.viewTransform().asTuple()
            QtCore.QTimer.singleShot(20, self.move_camera)
        except Exception:
            self.camera_error = traceback.format_exc()

    def check_camera_export(self):
        actual = self.camera.worldTransform().asTuple()
        expected = self.expected_camera_transform
        difference = max(abs(a - b) for a, b in zip(actual, expected))
        assert difference < 1e-4, ("Continuous viewport export matrix", difference, actual, expected)

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 170:
                raise TimeoutError("Camera/subdivision stress stalled")
            if self.camera_error:
                raise AssertionError(self.camera_error)
            entry = registry.get(self.key)
            if not entry:
                return
            if entry.error:
                raise AssertionError(entry.error)
            status = entry.session.status()
            if status["state"] == "failed":
                raise AssertionError(status["error"])
            frame = entry.session.frame_info()
            self.report["current"] = dict(stage=self.stage, status=status)
            self.save()
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                assert status["rendering"]["samples"] == self.samples, status["rendering"]
                if self.report["settings"]["denoising"]:
                    assert status["denoiser"] != "none", (
                        "Denoised navigation fixture did not enable its actual renderer denoiser"
                    )
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                self.entry = entry
                self.event("Million-triangle FX scene renders in a single viewer", entry)
                # The turbine owns renderer subdivision. Other million-triangle
                # SOP components remain independent, as in the artist showcase.
                hou.node("/obj/courtyard_turbine").parm("cycles_subdivision_pixel_size").set(0.75)
                self.before = status["generation"]
                self.stage = "subdivision-start"
                self.report["phase"] = "edit-object-subdivision"
            elif self.stage == "subdivision-start":
                if not self.ready(entry) or status["subdivision_control_faces"] == 0:
                    return
                self.diced_status = status
                self.before = status["generation"]
                self.event("Independent turbine adaptive subdivision has a usable image", entry)
                viewport = self.scene.curViewport()
                viewport.lockCameraToView(True)
                viewport.exportViewToCameraContinuously(True)
                assert viewport.camera() == self.camera and viewport.isCameraLockedToView()
                assert viewport.isViewExportedToCameraContinuously()
                self.report["continuous_camera_export"] = True
                self.viewport_translation = viewport.defaultCamera().translation()
                self.report["initial_view_translation"] = tuple(self.viewport_translation)
                self.report["initial_camera_translation"] = self.camera.parmTuple("t").eval()
                self.camera.setCurrent(True, clear_all_selected=True)
                self.drag_window = hou.qt.floatingPanelWindow(self.scene.floatingPanel())
                self.drag_position = QtCore.QPoint(self.drag_window.width() // 2, self.drag_window.height() // 2)
                QtTest.QTest.mousePress(
                    self.drag_window, QtCore.Qt.MiddleButton, QtCore.Qt.AltModifier, self.drag_position
                )
                assert QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton
                self.stage = "camera-drag"
                self.report["phase"] = "continuous-camera-export"
                self.move_camera()
            elif self.stage == "camera-drag":
                if self.camera_edits < 60:
                    assert status["subdivision_tessellations"] == self.diced_status["subdivision_tessellations"], status
                    return
                self.stage = "camera-hold"
                self.report["phase"] = "hold-camera-stationary"
            elif self.stage == "camera-hold":
                if (
                    not self.ready(entry)
                    or entry.camera_refinement_pending
                    or entry.change_serial > entry.applied_serial
                ):
                    return
                if status["subdivision_tessellations"] == self.diced_status["subdivision_tessellations"]:
                    return
                self.check_camera_export()
                assert QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton
                assert status["subdivision_tessellations"] == self.diced_status["subdivision_tessellations"] + 1
                assert status["geometry_conversions"] == self.diced_status["geometry_conversions"]
                self.event("Camera refines after 60 exports while the mouse is still held", entry)
                self.before = status["generation"]
                self.stage = "camera-settle"
                QtTest.QTest.mouseRelease(
                    self.drag_window, QtCore.Qt.MiddleButton, QtCore.Qt.AltModifier, self.drag_position
                )
                assert QtWidgets.QApplication.mouseButtons() == QtCore.Qt.NoButton
                self.rop.parm("samples").set(8)
                self.report["phase"] = "settle-camera-subdivision"
            elif self.stage == "camera-settle":
                if (
                    status["generation"] <= self.before
                    or status["state"] != "complete"
                    or not frame
                    or not frame["complete"]
                    or frame["generation"] != status["generation"]
                ):
                    return
                if entry.camera_refinement_pending:
                    return
                # The sampling change can finish before the release-refinement
                # notification has been submitted by the panel's host callback.
                if entry.change_serial > entry.applied_serial:
                    return
                if status["subdivision_tessellations"] == self.diced_status["subdivision_tessellations"]:
                    return
                self.event("Final camera result finishes after continuous edits", entry)
                assert entry is self.entry and len(registry.entries()) == 1
                assert status["subdivision_control_faces"] > 0
                assert status["subdivision_tessellations"] == self.diced_status["subdivision_tessellations"] + 1
                assert status["geometry_conversions"] == self.diced_status["geometry_conversions"]
                if self.report["settings"]["denoising"]:
                    # Cycles may meet its navigation budget at full resolution.
                    # Require feedback, not a particular scheduler choice. The
                    # progressive-layout pixel test requires coarse coverage.
                    self.report["coarse_preview_observed"] = bool(self.report.get("coarse_preview"))
                    assert self.report.get("navigation_preview"), (
                        "No low-sample or reduced-resolution denoised preview was painted during camera input"
                    )
                self.panel.grab().save(str(self.path.with_suffix(".camera-subdivision.png")))
                self.finish()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            if self.drag_window:
                QtTest.QTest.mouseRelease(
                    self.drag_window, QtCore.Qt.MiddleButton, QtCore.Qt.AltModifier, self.drag_position
                )
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            try:
                registry.close_all()
            except Exception:
                pass
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = CameraSubdivision()
