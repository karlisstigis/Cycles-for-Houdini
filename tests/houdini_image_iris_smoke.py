# SPDX-License-Identifier: Apache-2.0
"""One live Cycles Viewer: native image iris, exposure, file edits and automatic repairs.

Tests actual IPR polling and worker file revision checks. Parameter edits use
HOM; this does not certify physical mouse latency or display compositing.
"""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import camera_properties, device_selection, image_iris, native_scene, registry


class Workflow:
    width, height = 128, 96

    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mask_path = self.path.parent / "live-iris.exr"
        self.radiance = np.array((0.2, 0.4, 0.8), np.float32)
        self.write_mask("asymmetric")
        hou.setFrame(1)
        self.obj = hou.node("/obj").createNode("geo", "iris_target", run_init_scripts=False)
        self.sop = self.obj.createNode("grid", "small_emitter")
        self.sop.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.sop.parmTuple("size").set((0.08, 0.08))
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "iris_camera")
        self.camera.setParms(dict(tz=5, focus=2.5, fstop=0.05))
        camera_properties.install(self.camera)
        assert not camera_properties.install(self.camera)
        self.rop = hou.node("/out").createNode("cycles_ipr", "image_iris")
        requested = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        device_selection.set_devices(self.rop, json.loads(requested) if requested.startswith("[") else requested)
        self.rop.setParms(
            dict(
                source_mode="sop",
                soppath=self.sop.path(),
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=4096,
                ipr_samples=4096,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                material_kind="emission",
            )
        )
        self.rop.parmTuple("basecolor").set(tuple(float(v) for v in self.radiance))
        self.rop.parmTuple("background").set(tuple(float(v) for v in self.radiance))
        self.camera.setParms(
            dict(
                cycles_dof=1,
                cycles_image_iris=0,
                cycles_image_iris_file=str(self.mask_path),
                cycles_image_iris_method="hierarchical",
            )
        )
        if not image_iris.supported():
            assert self.camera.parmTemplateGroup().find("cycles_image_iris").isHidden()
            self.camera.parm("cycles_image_iris").set(1)
            try:
                native_scene.capture(self.rop)
            except ValueError as error:
                assert "does not include" in str(error), error
            else:
                raise AssertionError("Disabled build silently accepted image iris")
            self.path.write_text(
                json.dumps(dict(result="passed", feature_enabled=False, unsupported_request_rejected=True), indent=2)
            )
            hou.ui.postEventCallback(lambda: hou.exit(0, suppress_save_prompt=True))
            return
        assert not self.camera.parmTemplateGroup().find("cycles_image_iris").isHidden()
        self.steps = []

        def add(label, action, *, field=False, reject=False, reuse=False):
            self.steps.append(dict(label=label, action=action, field=field, reject=reject, reuse=reuse))

        add("Analytic constant field", lambda: None, field=True)
        add("Hierarchical constant field", lambda: self.camera.parm("cycles_image_iris").set(1), field=True)
        add(
            "CDF constant field",
            lambda: self.camera.parm("cycles_image_iris_method").set("cdf"),
            field=True,
            reuse=True,
        )
        add("Scaled mask preserves exposure", lambda: self.write_mask("asymmetric", scale=0.125), field=True)
        add("CDF bokeh", lambda: self.rop.parmTuple("background").set((0, 0, 0)), reuse=True)
        add("Hierarchical bokeh", lambda: self.camera.parm("cycles_image_iris_method").set("hierarchical"), reuse=True)
        add("Rotation uses cached image", lambda: self.camera.parm("cycles_aperture_rotation").set(90), reuse=True)
        add("Ratio uses cached image", lambda: self.camera.parm("cycles_aperture_ratio").set(2), reuse=True)
        add("Focus uses cached image", lambda: self.camera.parm("focus").set(3), reuse=True)
        add(
            "Restore optics uses cached image",
            lambda: self.camera.setParms(dict(focus=2.5, cycles_aperture_rotation=0, cycles_aperture_ratio=1)),
            reuse=True,
        )
        add("Live same-path ring image", lambda: self.write_mask("ring"))
        add("Missing image retains accepted render", lambda: self.mask_path.unlink(), reject=True, reuse=True)
        add("Missing image repaired without parameter edit", lambda: self.write_mask("asymmetric"))
        add(
            "Corrupt image retains accepted render",
            lambda: self.mask_path.write_bytes(b"not an EXR image"),
            reject=True,
            reuse=True,
        )
        add("Corrupt image repaired without parameter edit", lambda: self.write_mask("ring"))
        add("Black image retains accepted render", lambda: self.write_mask("black"), reject=True, reuse=True)
        add("Black image repaired without parameter edit", lambda: self.write_mask("asymmetric"))
        add("Disable image iris", lambda: self.camera.parm("cycles_image_iris").set(0))
        self.report = dict(
            result="running",
            device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"],
            scope=__doc__,
            events=[],
            tick_intervals=[],
        )
        self.index = 0
        self.panel = None
        self.busy = self.finished = False
        self.started = self.edited_at = self.last_tick = time.monotonic()
        self.phase = "mount"
        self.previous = None
        self.images = {}
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(920, 640))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def write_mask(self, kind, scale=1.0):
        y, x = np.mgrid[-1:1:65j, -1:1:65j]
        if kind == "asymmetric":
            mask = (
                ((x < -0.15) & (x > -0.8) & (abs(y) < 0.75)) | ((y < -0.1) & (y > -0.7) & (x < 0.65) & (x > -0.8))
            ).astype(np.float32)
        elif kind == "ring":
            mask = ((x * x + y * y > 0.35) & (x * x + y * y < 0.75)).astype(np.float32)
        elif kind == "black":
            mask = np.zeros_like(x, dtype=np.float32)
        else:
            raise ValueError(kind)
        mask[[0, -1], :] = 0
        mask[:, [0, -1]] = 0
        writer = oiio.ImageOutput.create(str(self.mask_path))
        assert writer
        assert writer.open(str(self.mask_path), oiio.ImageSpec(65, 65, 1, oiio.FLOAT))
        # OIIO's NumPy writer requires explicit (height, width, channels).
        assert writer.write_image(np.ascontiguousarray(mask * scale, dtype=np.float32)[:, :, None])
        assert writer.close()

    def save(self):
        self.report["phase"] = self.phase
        self.report["index"] = self.index
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    @staticmethod
    def plane(frame):
        plane = frame["planes"]["combined"]
        return (
            np.frombuffer(plane["pixels"], np.float32)
            .reshape(frame["height"], frame["width"], plane["channels"])[:, :, :3]
            .copy()
        )

    @staticmethod
    def moments(image):
        weights = image[:, :, 0].astype(np.float64)
        flux = weights.sum()
        assert flux > 0
        y, x = np.indices(weights.shape)
        return dict(flux=float(flux), centroid=[float((x * weights).sum() / flux), float((y * weights).sum() / flux)])

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["tick_intervals"].append(now - self.last_tick)
            self.last_tick = now
            assert now - self.started < 420, (self.phase, self.index)
            assert now - self.edited_at < 65, (self.steps[self.index]["label"], self.report.get("waiting"))
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.auto.setChecked(True)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            step = self.steps[self.index]
            self.report["waiting"] = dict(label=step["label"], status=status, frame=info)
            if now - self.edited_at < 0.35 or status.get("capture_pending") or status.get("pending_update"):
                return
            if step["reject"]:
                if not status.get("update_rejected"):
                    return
                assert not status.get("capture_restart_required"), status
                assert "iris" in status["error"].lower(), status
                assert info and info["generation"] == self.previous["generation"], (info, self.previous)
                pixels = self.previous_pixels
            else:
                assert not entry.error, (entry.error, status)
                if status.get("update_rejected"):
                    return
                assert status["state"] != "failed", status
                if not info or not info["complete"] or info["generation"] != status["generation"]:
                    return
                if self.previous and info["generation"] <= self.previous["generation"]:
                    return
                if (
                    self.panel._live_generation != info["generation"]
                    or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
                ):
                    return
                pixels = self.plane(entry.session.frame())
                assert np.isfinite(pixels).all()
                if step["field"]:
                    # Thousands of FP32 film additions have measurable rounding.
                    # Compare iris modes to the actual analytic exposure rather
                    # than mistaking that shared accumulation error for shading.
                    np.testing.assert_allclose(pixels, np.broadcast_to(self.radiance, pixels.shape), atol=6e-5, rtol=0)
                    if not hasattr(self, "field_reference"):
                        self.field_reference = pixels.copy()
                    else:
                        np.testing.assert_allclose(pixels, self.field_reference, atol=2e-6, rtol=0)
                else:
                    assert pixels.max() > 0.001 and pixels.mean() < 0.02, pixels.max()
                    self.images[step["label"]] = pixels
                    if step["label"] == "Hierarchical bokeh":
                        a, b = self.moments(self.images["CDF bokeh"]), self.moments(pixels)
                        assert abs(a["flux"] / b["flux"] - 1) < 0.15, (a, b)
                        assert np.linalg.norm(np.array(a["centroid"]) - b["centroid"]) < 0.8, (a, b)
                        self.report["sampling_comparison"] = dict(cdf=a, hierarchical=b)
                    if step["label"] == "Rotation uses cached image":
                        a, b = self.moments(self.images["Hierarchical bokeh"]), self.moments(pixels)
                        assert np.linalg.norm(np.array(a["centroid"]) - b["centroid"]) > 1, (a, b)
            event = dict(
                label=step["label"],
                rejected=step["reject"],
                status=status,
                generation=info["generation"],
                cooks=self.sop.cookCount(),
            )
            if not step["field"] and not step["reject"]:
                event["image"] = self.moments(pixels)
            if self.previous:
                assert status["renderer_pid"] == self.previous["status"]["renderer_pid"], event
                assert event["cooks"] == self.previous["cooks"], event
                for key in ("geometry_conversions", "extractions", "subdivision_tessellations"):
                    assert status[key] == self.previous["status"][key], (key, event, self.previous)
                if step["reuse"]:
                    assert status["image_iris_preparations"] == self.previous["status"]["image_iris_preparations"], (
                        event
                    )
            assert len(registry.entries()) == 1
            self.report["events"].append(event)
            self.previous = event
            self.previous_pixels = pixels
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                self.finish()
                return
            self.steps[self.index]["action"]()
            self.edited_at = time.monotonic()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        self.finished = True
        if self.callback in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.callback)
        if self.panel:
            if not error:
                self.panel.grab().save(str(self.path.with_suffix(".png")))
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
            self.panel.dispose()
        registry.close_all()
        if self.images:
            np.savez_compressed(
                self.path.with_suffix(".npz"), **{name.replace(" ", "_"): image for name, image in self.images.items()}
            )
        self.report.update(result="failed" if error else "passed", error=error)
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
