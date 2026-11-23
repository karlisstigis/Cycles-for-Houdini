# SPDX-License-Identifier: Apache-2.0
"""Live Viewer RGB iris transmission, neutral exposure, file edits and repairs.

Uses actual IPR polling and immutable worker image resources. Parameter changes
use HOM; this does not measure physical mouse latency or desktop composition.
"""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
from PySide6 import QtCore, QtTest, QtGui, QtWidgets
from cycles_houdini import camera_properties, device_selection, image_iris, native_scene, registry


class Workflow:
    width, height = 128, 96

    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mask_path = self.path.parent / "live-colored-iris.exr"
        self.radiance = np.array((0.2, 0.4, 0.8), np.float32)
        self.write_mask("rgb")
        hou.setFrame(1)
        self.obj = hou.node("/obj").createNode("geo", "colored_iris_target", run_init_scripts=False)
        self.sop = self.obj.createNode("grid", "small_emitter")
        self.sop.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.sop.parmTuple("size").set((0.08, 0.08))
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "colored_iris_camera")
        self.camera.setParms(dict(tz=5, focus=2.5, fstop=0.05))
        camera_properties.install(self.camera)
        assert not camera_properties.install(self.camera)
        assert self.camera.parm("cycles_image_iris_color").eval() == 0
        assert self.camera.parm("cycles_image_iris_method").evalAsString() == "cdf"
        # Header toggles own their controls; moving old flat controls must keep
        # their authored values and expressions when the camera tab is refreshed.
        group = self.camera.parmTemplateGroup()
        dof = group.find("cycles_camera_dof")
        iris = group.find("cycles_camera_image_iris")
        assert dof.tags().get("sidefx::header_toggle") == "cycles_dof"
        assert iris.tags().get("sidefx::header_toggle") == "cycles_image_iris"
        assert group.containingFolder("cycles_camera_image_iris").name() == "cycles_camera_dof"
        self.camera.parm("cycles_aperture_ratio").setExpression("1.5", hou.exprLanguage.Hscript)
        self.camera.parm("cycles_image_iris_color").set(1)
        dof.setParmTemplates([p for p in dof.parmTemplates() if p.name() != iris.name()] + list(iris.parmTemplates()))
        dof.setTags({"group_default": "1"})
        group.replace("cycles_camera_dof", dof)
        self.camera.setParmTemplateGroup(group)
        assert camera_properties.install(self.camera) and not camera_properties.install(self.camera)
        assert self.camera.parm("cycles_aperture_ratio").expression() == "1.5"
        assert self.camera.parm("cycles_image_iris_color").eval() == 1
        ratio = self.camera.parm("cycles_aperture_ratio")
        ratio.deleteAllKeyframes()
        ratio.set(4)
        assert ratio.eval() == 4 and ratio.parmTemplate().minValue() == 0.01 and ratio.parmTemplate().maxValue() == 2
        assert ratio.parmTemplate().minIsStrict() and not ratio.parmTemplate().maxIsStrict()
        self.camera.setParms(
            dict(cycles_dof=1, cycles_image_iris=0, cycles_image_iris_color=0, cycles_aperture_ratio=1)
        )
        self.camera.updateParmStates()
        assert not self.camera.parm("cycles_aperture_blades").isDisabled()
        assert self.camera.parm("cycles_image_iris_file").isDisabled()
        self.camera.parm("cycles_image_iris").set(1)
        self.camera.updateParmStates()
        assert self.camera.parm("cycles_aperture_blades").isDisabled()
        assert not self.camera.parm("cycles_image_iris_color").isDisabled()
        assert not self.camera.parm("cycles_aperture_ratio").isDisabled()
        self.camera.parm("cycles_dof").set(0)
        self.camera.updateParmStates()
        assert not self.camera.parm("cycles_dof").isDisabled()
        assert self.camera.parm("cycles_focus_distance").isDisabled()
        assert self.camera.parm("cycles_image_iris").isDisabled()
        assert self.camera.parm("cycles_image_iris_method").isDisabled()
        self.camera.setParms(dict(cycles_dof=1, cycles_image_iris=0))
        # A camera saved before RGB support gets the new control on refresh.
        group = self.camera.parmTemplateGroup()
        group.remove("cycles_image_iris_color")
        self.camera.setParmTemplateGroup(group)
        assert camera_properties.install(self.camera)
        assert not camera_properties.install(self.camera)
        template = self.camera.parmTemplateGroup().find("cycles_image_iris_color")
        assert template.defaultValue() in (False, (False,))
        assert "cycles_image_iris == 0" in str(template.conditionals())
        self.rop = hou.node("/out").createNode("cycles_ipr", "colored_image_iris")
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
        self.camera.setParms(dict(cycles_dof=1, cycles_image_iris=0, cycles_image_iris_file=str(self.mask_path)))
        if not image_iris.supported():
            assert template.isHidden()
            self.camera.setParms(dict(cycles_image_iris=1, cycles_image_iris_color=1))
            try:
                native_scene.capture(self.rop)
            except ValueError as error:
                assert "does not include" in str(error), error
            else:
                raise AssertionError("Disabled build silently accepted colored image iris")
            self.path.write_text(
                json.dumps(dict(result="passed", feature_enabled=False, unsupported_request_rejected=True), indent=2)
            )
            hou.ui.postEventCallback(lambda: hou.exit(0, suppress_save_prompt=True))
            return
        assert not template.isHidden()
        self.steps = []

        def add(label, action, *, field=None, reject=False, reuse=False, active=False):
            self.steps.append(dict(label=label, action=action, field=field, reject=reject, reuse=reuse, active=active))

        add("Analytic neutral field", lambda: None, field="neutral")
        add("RGB image color disabled", self.enable_image, field="neutral")
        add(
            "CDF RGB transmission",
            lambda: self.camera.parm("cycles_image_iris_color").set(1),
            field="color",
            reuse=True,
        )
        add(
            "Hierarchical RGB transmission",
            lambda: self.camera.parm("cycles_image_iris_method").set("hierarchical"),
            field="color",
            reuse=True,
        )
        add("Neutral RGB keeps exposure", lambda: self.write_mask("neutral"), field="neutral")
        add("Grayscale remains neutral", lambda: self.write_mask("gray"), field="neutral")
        add("RGBA mask applies alpha once", lambda: self.write_mask("rgba"), field="color")
        add("Return to RGB mask", lambda: self.write_mask("rgb"), field="color")
        add(
            "Disable color keeps exposure",
            lambda: self.camera.parm("cycles_image_iris_color").set(0),
            field="neutral",
            reuse=True,
        )
        add("Colored bokeh", self.start_bokeh, reuse=True)
        add("CDF colored bokeh", lambda: self.camera.parm("cycles_image_iris_method").set("cdf"), reuse=True)
        add("Monochrome bokeh", lambda: self.camera.parm("cycles_image_iris_color").set(0), reuse=True)
        add("Restore colored bokeh", lambda: self.camera.parm("cycles_image_iris_color").set(1), reuse=True)
        add("Image replaced during accumulation", self.arm_active_change, active=True)
        add("Missing RGB image retains render", lambda: self.mask_path.unlink(), reject=True, reuse=True)
        add("Missing RGB image repaired by file only", lambda: self.write_mask("rgb"))
        add(
            "Corrupt RGB image retains render",
            lambda: self.mask_path.write_bytes(b"not an image"),
            reject=True,
            reuse=True,
        )
        add("Corrupt RGB image repaired by file only", lambda: self.write_mask("flipped"))
        add("Black RGB image retains render", lambda: self.write_mask("black"), reject=True, reuse=True)
        add("Black RGB image repaired by file only", lambda: self.write_mask("rgb"))
        add("Negative RGB image retains render", lambda: self.write_mask("negative"), reject=True, reuse=True)
        add("Negative RGB image repaired by file only", lambda: self.write_mask("rgba"))
        self.report = dict(result="running", device=requested, scope=__doc__, events=[], tick_intervals=[])
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

    def write_mask(self, kind):
        y, x = np.mgrid[-1:1:65j, -1:1:65j]
        shape = ((x * x + y * y) < 0.8).astype(np.float32)
        rgb = (
            np.stack(
                (np.where(x < -0.1, 1.0, 0.06), np.where(y < -0.1, 0.7, 0.08), np.where(x > 0.1, 1.0, 0.03)), axis=2
            ).astype(np.float32)
            * shape[:, :, None]
        )
        if kind == "flipped":
            rgb = rgb[:, ::-1, :].copy()
        if kind == "neutral":
            rgb = np.repeat(shape[:, :, None], 3, axis=2) * 0.3
        if kind == "gray":
            pixels = shape[:, :, None] * 0.3
        elif kind == "rgba":
            pixels = np.concatenate((rgb, ((x + 1) * 0.5).astype(np.float32)[:, :, None]), axis=2)
        elif kind == "black":
            pixels = np.zeros_like(rgb)
        elif kind == "negative":
            pixels = rgb.copy()
            pixels[30, 30, 0] = -0.1
        else:
            pixels = rgb
        effective = rgb if pixels.shape[2] != 1 else np.repeat(pixels, 3, axis=2)
        if pixels.shape[2] == 4:
            effective = effective * pixels[:, :, 3:4]
        # Integrate the exact bilinear reconstruction, including edge weights.
        integral = (effective[:-1, :-1] + effective[1:, :-1] + effective[:-1, 1:] + effective[1:, 1:]).sum(
            axis=(0, 1), dtype=np.float64
        ) * 0.25
        denominator = float(integral @ np.array((0.2126, 0.7152, 0.0722)))
        self.expected_color = integral / denominator if denominator > 0 else np.zeros(3)
        writer = oiio.ImageOutput.create(str(self.mask_path))
        assert writer
        spec = oiio.ImageSpec(65, 65, pixels.shape[2], oiio.FLOAT)
        if pixels.shape[2] == 4:
            spec.alpha_channel = 3
        assert writer.open(str(self.mask_path), spec)
        assert writer.write_image(np.ascontiguousarray(pixels, dtype=np.float32))
        assert writer.close()

    def enable_image(self):
        self.camera.setParms(dict(cycles_image_iris=1, cycles_aperture_blades=1))

    def start_bokeh(self):
        self.camera.parm("cycles_aperture_blades").set(2)
        self.camera.parm("cycles_image_iris_color").set(1)
        self.rop.parmTuple("background").set((0, 0, 0))

    def arm_active_change(self):
        self.rop.parm("ipr_samples").set(262144)
        self.camera.parm("focus").set(2.6)
        self.phase = "arm"

    def save(self):
        self.report.update(phase=self.phase, index=self.index)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    @staticmethod
    def pixels(frame):
        plane = frame["planes"]["combined"]
        return (
            np.frombuffer(plane["pixels"], np.float32)
            .reshape(frame["height"], frame["width"], plane["channels"])
            .copy()
        )

    @staticmethod
    def moments(image):
        y, x = np.indices(image.shape[:2])
        centroids = []
        flux = []
        for channel in range(3):
            weights = image[:, :, channel].astype(np.float64)
            total = weights.sum()
            assert total > 0
            flux.append(float(total))
            centroids.append([float((x * weights).sum() / total), float((y * weights).sum() / total)])
        return dict(flux=flux, centroids=centroids)

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["tick_intervals"].append(now - self.last_tick)
            self.last_tick = now
            assert now - self.started < 480, (self.phase, self.index)
            assert now - self.edited_at < 75, (self.steps[self.index]["label"], self.report.get("waiting"))
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
            if self.phase == "arm":
                if not info or info["complete"] or info["generation"] <= self.previous["generation"]:
                    return
                self.report["edit_during_accumulation"] = dict(status=status, frame=info)
                self.write_mask("flipped")
                self.rop.parm("ipr_samples").set(4096)
                self.camera.parm("focus").set(2.5)
                self.phase = "wait"
                self.edited_at = now
                return
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
                if self.index == 0 and not self.report.get("viewer_color_controls"):
                    generation = status["generation"]
                    raw = entry.session.frame_info()
                    for control in (self.panel.exposure, self.panel.gamma):
                        control.setValue(1.374)
                        assert control.value() == 1.37
                        line = control.input.findChildren(QtWidgets.QLineEdit)[0]
                        line.setFocus()
                        line.selectAll()
                        QtTest.QTest.keyClicks(line, "1.37")
                        QtTest.QTest.keyClick(line, QtCore.Qt.Key_Return)
                        assert control.value() == 1.37 and line.text() == "1.37"
                        QtTest.QTest.mouseClick(line, QtCore.Qt.MiddleButton, QtCore.Qt.ControlModifier)
                        assert control.value() == control.default
                    swatch = self.panel.background_button
                    assert swatch.swatch.size() == self.panel.checker.size()
                    for control in (
                        self.panel.checker,
                        swatch,
                        swatch.swatch,
                        self.panel.actual_pixels,
                        self.panel.fit_button,
                    ):
                        assert control.size() == self.panel.checker.size() and control.width() == control.height(), (
                            control.size()
                        )
                        assert (
                            control.parentWidget().height()
                            == self.panel.channel_buttons["rgba"].parentWidget().height()
                        )
                    QtTest.QTest.mouseClick(swatch.swatch, QtCore.Qt.LeftButton)
                    assert swatch.dialog and not self.panel.checker.isChecked()
                    swatch.dialog.setCurrentColor(QtGui.QColor("#286cb0"))
                    swatch.dialog.reject()
                    assert self.panel.checker.isChecked() and self.panel.canvas.background_color is None
                    QtTest.QTest.mouseClick(swatch.swatch, QtCore.Qt.LeftButton)
                    swatch.dialog.setCurrentColor(QtGui.QColor("#286cb0"))
                    swatch.dialog.accept()
                    assert (
                        self.panel.canvas.background_color == QtGui.QColor("#286cb0")
                        and not self.panel.checker.isChecked()
                    )
                    painted = swatch.swatch.grab().toImage()
                    # Check near the edge as well as the center: the entire segment is the swatch.
                    for x in (2, painted.width() // 2, painted.width() - 3):
                        assert painted.pixelColor(x, painted.height() // 2) == QtGui.QColor("#286cb0"), (
                            x,
                            painted.size(),
                            painted.pixelColor(x, painted.height() // 2).name(),
                        )
                    QtTest.QTest.mouseClick(swatch, QtCore.Qt.LeftButton)
                    assert not swatch.isChecked() and not self.panel.canvas.background_enabled and swatch.dialog is None
                    QtTest.QTest.mouseClick(swatch, QtCore.Qt.LeftButton)
                    assert swatch.isChecked() and self.panel.canvas.background_enabled and swatch.dialog is None
                    QtTest.QTest.mouseClick(self.panel.checker, QtCore.Qt.LeftButton)
                    assert not swatch.isChecked() and self.panel.checker.isChecked()
                    QtTest.QTest.mouseClick(self.panel.checker, QtCore.Qt.LeftButton)
                    assert (
                        not swatch.isChecked()
                        and not self.panel.checker.isChecked()
                        and not self.panel.canvas.background_enabled
                    )
                    assert self.panel.canvas.background_color == QtGui.QColor("#286cb0")
                    assert entry.session.status()["generation"] == generation and entry.session.frame_info() == raw
                    self.report["viewer_color_controls"] = (
                        "Native fields/reset, separate BG/swatch actions, checker/color/default transitions and retained color; renderer unchanged"
                    )
                pixels = self.pixels(entry.session.frame())
                assert np.isfinite(pixels).all()
                assert pixels.shape[2] == 4, pixels.shape
                np.testing.assert_allclose(pixels[:, :, 3], 1.0, atol=2e-5, rtol=0)
                if step["field"]:
                    target = self.radiance * (self.expected_color if step["field"] == "color" else 1.0)
                    mean = pixels[:, :, :3].mean(axis=(0, 1), dtype=np.float64)
                    np.testing.assert_allclose(mean, target, atol=0.003, rtol=0.015)
                    self.report.setdefault("field_comparisons", []).append(
                        dict(label=step["label"], expected=target.tolist(), observed=mean.tolist())
                    )
                    if step["field"] == "neutral":
                        np.testing.assert_allclose(
                            pixels[:, :, :3], np.broadcast_to(self.radiance, pixels[:, :, :3].shape), atol=6e-5, rtol=0
                        )
                else:
                    assert pixels[:, :, :3].max() > 0.001 and pixels[:, :, :3].mean() < 0.04
                    self.images[step["label"]] = pixels
                    moments = self.moments(pixels)
                    if step["label"] == "Colored bokeh":
                        centers = np.array(moments["centroids"])
                        assert np.linalg.norm(centers[0] - centers[2]) > 1.0, moments
                    elif step["label"] == "Monochrome bokeh":
                        normalized = pixels[:, :, :3] / self.radiance
                        np.testing.assert_allclose(normalized[:, :, 0], normalized[:, :, 2], atol=1e-4, rtol=0)
                    elif step["label"] == "Image replaced during accumulation":
                        before = self.moments(self.images["Restore colored bokeh"])
                        assert np.linalg.norm(np.array(before["centroids"][0]) - moments["centroids"][0]) > 1.0, (
                            before,
                            moments,
                        )
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
