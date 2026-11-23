# SPDX-License-Identifier: Apache-2.0
"""Actual artist HIP frame changes timed through a Houdini UI redraw fence."""

import ctypes, hashlib, json, os, runpy, time, traceback
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtGui
from support.private_scenes import artist_scene


class Benchmark:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.original = artist_scene("Cycles_Test/Blender_Instancing.hiplc")
        self.sha = hashlib.sha256(self.original.read_bytes()).hexdigest()
        hou.hipFile.load(str(self.original), suppress_save_prompt=True, ignore_load_warnings=True)
        self.geo = hou.node("/obj/bottles1")
        self.source = self.geo.node("blender_reference2")
        self.output = self.geo.node("copytopoints1")
        self.native_control = os.getenv("CH_ARTIST_EXACT_NATIVE_CONTROL") == "1"
        if self.native_control:
            factory = runpy.run_path(
                str(
                    Path(__file__).resolve().parents[1]
                    / "artifacts/artist-hull-regression/exact_hull_native_control.py"
                )
            )["create_native_hull_control"]
            self.output = factory(self.source, self.output)
        self.target = self.geo.node("attribrandomize1")
        if "CH_ARTIST_MATCH_PIECE" in os.environ:
            self.output.parm("useidattrib").set(int(os.environ["CH_ARTIST_MATCH_PIECE"]))
        self.trace = None
        trace_path = os.getenv("CH_ARTIST_GT_TRACE_LIBRARY")
        trace_install = None
        if trace_path:
            self.trace = ctypes.CDLL(trace_path)
            self.trace.install_gt_trace.argtypes = [ctypes.c_char_p, ctypes.c_double]
            self.trace.install_gt_trace.restype = ctypes.c_char_p
            self.trace.gt_trace_state.argtypes = [ctypes.c_int]
            self.trace.gt_trace_state.restype = ctypes.c_char_p
            self.trace.uninstall_gt_trace.argtypes = []
            self.trace.uninstall_gt_trace.restype = None
            trace_install = json.loads(self.trace.install_gt_trace(self.output.path().encode(), hou.time()))
        assert self.output.evalParm("idattrib") == "name"
        self.piece_enabled = bool(self.output.evalParm("useidattrib"))
        assert self.source.evalParm("max_hull_vertices") == 16
        for obj in hou.node("/obj").children():
            if hasattr(obj, "setDisplayFlag"):
                obj.setDisplayFlag(obj == self.geo)
        for pane in hou.ui.curDesktop().paneTabs():
            if pane.type() == hou.paneTabType.SceneViewer:
                pane.setType(hou.paneTabType.NetworkEditor)
        self.output.setDisplayFlag(True)
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(1700, 1000))
        self.viewer.setPwd(self.geo)
        self.viewer.setCurrentNode(self.output)
        self.viewport = self.viewer.curViewport()
        self.viewport.changeType(hou.geometryViewportType.Perspective)
        self.window = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
        screen = next(s for s in QtGui.QGuiApplication.screens() if "UP2720Q" in (s.name() + s.model()).upper())
        self.screen = screen
        self.window.showNormal()
        self.window.windowHandle().setScreen(screen)
        area = screen.availableGeometry()
        self.window.setGeometry(
            area.left() + 35, area.top() + 50, min(1800, area.width() - 70), min(1100, area.height() - 100)
        )
        self.window.raise_()
        hou.setFrame(1)
        self.viewport.frameBoundingBox(self.output.geometry().boundingBox())
        self.report = dict(
            result="running",
            product_acceptance=False,
            original=str(self.original),
            original_sha256=self.sha,
            saved=False,
            screen=screen.name(),
            samples=[],
            mode_setup=[],
            output_path=self.output.path(),
            exact_native_hull_control=self.native_control,
            metric="UI redraw-completion wall time: setFrame to callback following postRedrawFence",
            hardware_gpu_timestamp=False,
            fixed_sleep_seconds=0,
            gt_trace_enabled=bool(self.trace),
            gt_trace_install=trace_install,
            settings=dict(
                seed=self.target.evalParm("seed"),
                piece_attribute="name",
                piece_enabled=self.piece_enabled,
                cap=self.source.evalParm("max_hull_vertices"),
                shape_frame=self.source.evalParm("shape_frame"),
                source_frame=self.source.evalParm("ref_frame1"),
            ),
        )
        self.modes = os.getenv("CH_ARTIST_FRAME_MODES", "bounds,hull").split(",")
        self.mode_index = 0
        self.mode = self.modes[0]
        self.delayed_diagnostics = os.getenv("CH_ARTIST_DELAYED_DIAGNOSTICS") == "1"
        self.frames = [int(value) for value in os.getenv("CH_ARTIST_FRAME_SEQUENCE", "18,1").split(",")]
        self.trials = int(os.getenv("CH_ARTIST_FRAME_TRIALS", "3"))
        self.report.update(frame_sequence=self.frames, trials=self.trials)
        self.present_match = os.getenv("CH_ARTIST_PRESENT_MATCH") == "1"
        self.report["presentation_reference_matching"] = self.present_match
        if self.present_match:
            self.report["metric"] = (
                "Visible target-frame wall time: setFrame until screen silhouette matches independently settled target-frame reference"
            )
            self.report["reference_calibration_delay_seconds_excluded_from_samples"] = 0.25
            self.report["presentation_poll_interval_ms"] = 16
            self.report["presentation_timeout_seconds"] = 2
        self.trial = 0
        self.direction = 0
        self.save()
        hou.ui.postEventCallback(self.prepare_mode)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def packed_snapshot(self):
        result = {}
        for label, node in (("source", self.source), ("output", self.output)):
            geometry = node.geometry()
            box = geometry.boundingBox()
            samples = {}
            for primitive in geometry.prims():
                name = primitive.attribValue("name")
                if name in samples:
                    continue
                hull = primitive.intrinsicValue("cycles_blender_viewport_hull")
                primitive_box = primitive.boundingBox()
                samples[name] = dict(
                    detail_id=primitive.intrinsicValue("cycles_blender_viewport_detail_id"),
                    hull_length=len(hull),
                    hull_sha256=hashlib.sha256(hull.encode()).hexdigest(),
                    bounds_status=primitive.intrinsicValue("cycles_blender_bounds_status"),
                    hull_points=primitive.intrinsicValue("cycles_blender_viewport_hull_points"),
                    bounds=list(primitive_box.minvec()) + list(primitive_box.maxvec()),
                    intrinsic_bounds=list(primitive.intrinsicValue("bounds")),
                )
                if len(samples) == 4:
                    break
            result[label] = dict(
                primitives=len(geometry.prims()),
                bbox=list(box.minvec()) + list(box.maxvec()),
                samples=samples,
                errors=node.errors(),
                warnings=node.warnings(),
            )
        return result

    def prepare_mode(self):
        try:
            self.setup_start = time.perf_counter()
            self.source.setParms(
                dict(
                    viewport_shape="bounds" if self.mode == "bounds" else "hull",
                    viewport_wireframe=int(self.mode == "wire"),
                )
            )
            hou.setFrame(1)
            self.output.geometry()
            self.viewport.draw()
            hou.ui.postRedrawFence()
            if self.present_match:
                self.references = {}
                self.calibration_frames = list(dict.fromkeys([1] + self.frames))
                self.calibration_index = 0
                self.calibrate_frame()
            else:
                hou.ui.postEventCallback(lambda: QtCore.QTimer.singleShot(0, self.mode_ready))
        except Exception:
            self.fail()

    def screen_mask(self, image):
        image = image.toImage().convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        pixels = np.frombuffer(image.bits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
        rgb = pixels[:, : image.width() * 4].reshape(image.height(), image.width(), 4)[:, :, :3]
        return np.all(rgb[300:970, 250:1500] >= 245, axis=2).copy()

    def calibrate_frame(self):
        frame = self.calibration_frames[self.calibration_index]
        self.calibration_start = time.perf_counter()
        self.calibration_explicit_retry = False
        hou.setFrame(frame)
        self.output.geometry()
        self.viewport.draw()
        hou.ui.postRedrawFence()
        QtCore.QTimer.singleShot(250, self.calibration_ready)

    def calibration_ready(self):
        try:
            frame = self.calibration_frames[self.calibration_index]
            image = self.screen.grabWindow(int(self.window.winId()))
            mask = self.screen_mask(image)
            filename = self.path.with_suffix(f".{self.mode}.reference.frame{frame}.png")
            assert image.save(str(filename))
            if int(mask.sum()) <= 10000:
                if time.perf_counter() - self.calibration_start < 2:
                    QtCore.QTimer.singleShot(16, self.calibration_ready)
                    return
                if not self.calibration_explicit_retry:
                    self.calibration_explicit_retry = True
                    image.save(
                        str(self.path.with_suffix(f".{self.mode}.screen_only_missing_before_redraw.frame{frame}.png"))
                    )
                    self.viewport.draw()
                    hou.ui.postRedrawFence()
                    QtCore.QTimer.singleShot(250, self.calibration_ready)
                    return
                self.report["calibration_failure"] = dict(
                    mode=self.mode,
                    frame=frame,
                    white_pixels=int(mask.sum()),
                    image=str(filename),
                    elapsed_seconds=time.perf_counter() - self.calibration_start,
                    widget_grab_used=False,
                )
                raise AssertionError("Reference contains no visible bottles after 2 seconds; screen-only capture")
            self.references[frame] = mask
            box = self.output.geometry().boundingBox()
            self.report.setdefault("presentation_references", []).append(
                dict(
                    mode=self.mode,
                    trial=self.trial,
                    frame=frame,
                    image=str(filename),
                    white_pixels=int(mask.sum()),
                    output_bounds=list(box.minvec()) + list(box.maxvec()),
                )
            )
            self.calibration_index += 1
            if self.calibration_index < len(self.calibration_frames):
                self.calibrate_frame()
            else:
                distinctions = {
                    str(frame): int(np.count_nonzero(mask != self.references[1]))
                    for frame, mask in self.references.items()
                    if frame != 1
                }
                assert all(value > 2000 for value in distinctions.values()), (
                    f"Reference frames are not distinct: {distinctions}"
                )
                self.report.setdefault("reference_distinctions", []).append(
                    dict(mode=self.mode, trial=self.trial, different_pixels_from_frame1=distinctions)
                )
                hou.setFrame(1)
                self.output.geometry()
                self.viewport.draw()
                hou.ui.postRedrawFence()
                QtCore.QTimer.singleShot(250, self.mode_ready)
        except Exception:
            self.fail()

    def mode_ready(self):
        self.report["mode_setup"].append(
            dict(mode=self.mode, trial=self.trial, redraw_seconds=time.perf_counter() - self.setup_start)
        )
        if os.getenv("CH_ARTIST_CAPTURE_START") == "1":
            filename = self.path.with_suffix(f".{self.mode}.initial.frame{int(hou.frame())}.png")
            assert self.screen.grabWindow(int(self.window.winId())).save(str(filename))
            self.report.setdefault("initial_captures", []).append(
                dict(frame=hou.frame(), image=str(filename), output_primitives=len(self.output.geometry().prims()))
            )
        self.save()
        self.direction = 0
        self.change_frame()

    def change_frame(self):
        try:
            target = self.frames[self.direction]
            if self.trace:
                self.trace.gt_trace_state(1)
            self.sample = dict(mode=self.mode, trial=self.trial, from_frame=hou.frame(), to_frame=target)
            self.presentation_polls = 0
            self.start = time.perf_counter()
            hou.setFrame(target)
            self.after_frame = time.perf_counter()
            geometry = self.output.geometry()
            self.after_cook = time.perf_counter()
            self.sample.update(
                set_frame_seconds=self.after_frame - self.start,
                geometry_fetch_seconds=self.after_cook - self.after_frame,
                output_last_cook_ms=self.output.lastCookTime(),
                target_last_cook_ms=self.target.lastCookTime(),
                output_primitives=len(geometry.prims()),
                target_points=len(self.target.geometry().points()),
            )
            expected = 4912 if self.piece_enabled else 4912 * len(self.source.geometry().prims())
            assert self.sample["output_primitives"] == expected, self.sample
            self.window.setWindowTitle(f"Artist frame benchmark — {self.mode} — frame {target}")
            self.viewport.draw()
            self.before_fence = time.perf_counter()
            hou.ui.postRedrawFence()
            hou.ui.postEventCallback(lambda: QtCore.QTimer.singleShot(0, self.redraw_complete))
        except Exception:
            self.fail()

    def redraw_complete(self):
        try:
            ended = time.perf_counter()
            self.sample.setdefault("first_fence_callback_seconds", ended - self.start)
            if self.present_match:
                before_copy = time.perf_counter()
                image = self.screen.grabWindow(int(self.window.winId()))
                after_copy = time.perf_counter()
                mask = self.screen_mask(image)
                difference = int(np.count_nonzero(mask != self.references[int(hou.frame())]))
                after_match = time.perf_counter()
                self.sample["screen_capture_total_seconds"] = (
                    self.sample.get("screen_capture_total_seconds", 0) + after_copy - before_copy
                )
                self.sample["screen_mask_match_total_seconds"] = (
                    self.sample.get("screen_mask_match_total_seconds", 0) + after_match - after_copy
                )
                self.presentation_polls += 1
                if difference > 1000:
                    if time.perf_counter() - self.start >= 2:
                        image.save(
                            str(self.path.with_suffix(f".{self.mode}.presentation_timeout.frame{int(hou.frame())}.png"))
                        )
                        raise AssertionError(f"Target frame not presented within 2 seconds: {difference} pixels differ")
                    QtCore.QTimer.singleShot(16, self.redraw_complete)
                    return
                ended = time.perf_counter()
                self.sample.update(
                    presentation_poll_count=self.presentation_polls,
                    visible_target_seconds=ended - self.start,
                    fence_to_target_other_seconds=ended
                    - self.start
                    - self.sample["first_fence_callback_seconds"]
                    - self.sample["screen_capture_total_seconds"]
                    - self.sample["screen_mask_match_total_seconds"],
                    screen_capture_and_match_included=True,
                    reference_mask_difference_pixels=difference,
                    reference_mask_tolerance_pixels=1000,
                    target_visible_white_pixels=int(mask.sum()),
                )
            self.sample.update(
                ui_redraw_completion_seconds=ended - self.start,
                after_cook_to_redraw_callback_seconds=ended - self.after_cook,
                fence_queue_seconds=ended - self.before_fence,
            )
            if self.trace:
                self.sample["gt_trace"] = json.loads(self.trace.gt_trace_state(0))
            if not self.present_match:
                image = self.screen.grabWindow(int(self.window.winId()))
                after_copy = time.perf_counter()
            filename = self.path.with_suffix(f".{self.mode}.trial{self.trial}.frame{int(hou.frame())}.png")
            assert image.save(str(filename))
            self.sample.update(
                image=str(filename),
                image_copy_seconds=max(0, after_copy - ended),
                png_write_seconds=time.perf_counter() - after_copy,
            )
            self.report["samples"].append(self.sample)
            if os.getenv("CH_ARTIST_H1_SNAPSHOT") == "1":
                self.sample["packed_snapshot"] = self.packed_snapshot()
            self.save()
            if self.direction < len(self.frames) - 1:
                self.direction += 1
                hou.ui.postEventCallback(self.change_frame)
            elif self.delayed_diagnostics and self.mode != "bounds":
                self.delay_start = time.perf_counter()
                QtCore.QTimer.singleShot(2000, self.capture_without_redraw)
            else:
                self.finish_mode()
        except Exception:
            self.fail()

    def capture_without_redraw(self):
        try:
            filename = self.path.with_suffix(f".{self.mode}.delayed_no_redraw.frame{int(hou.frame())}.png")
            image = self.screen.grabWindow(int(self.window.winId()))
            assert image.save(str(filename))
            self.report.setdefault("delayed_diagnostics", []).append(
                dict(
                    mode=self.mode,
                    frame=hou.frame(),
                    stage="after_delay_without_explicit_redraw",
                    delay_seconds=time.perf_counter() - self.delay_start,
                    image=str(filename),
                    included_in_timing_samples=False,
                    packed_snapshot=self.packed_snapshot() if os.getenv("CH_ARTIST_H1_SNAPSHOT") == "1" else None,
                )
            )
            self.save()
            self.viewport.draw()
            hou.ui.postRedrawFence()
            hou.ui.postEventCallback(self.capture_after_explicit_redraw)
        except Exception:
            self.fail()

    def capture_after_explicit_redraw(self):
        try:
            filename = self.path.with_suffix(f".{self.mode}.explicit_redraw.frame{int(hou.frame())}.png")
            assert self.screen.grabWindow(int(self.window.winId())).save(str(filename))
            self.report.setdefault("delayed_diagnostics", []).append(
                dict(
                    mode=self.mode,
                    frame=hou.frame(),
                    stage="after_explicit_redraw_fence",
                    image=str(filename),
                    included_in_timing_samples=False,
                    gt_trace=json.loads(self.trace.gt_trace_state(0)) if self.trace else None,
                )
            )
            self.save()
            self.finish_mode()
        except Exception:
            self.fail()

    def finish_mode(self):
        if self.mode_index < len(self.modes) - 1:
            self.mode_index += 1
            self.mode = self.modes[self.mode_index]
            hou.ui.postEventCallback(self.prepare_mode)
        elif self.trial < self.trials - 1:
            self.trial += 1
            self.mode_index = 0
            self.mode = self.modes[0]
            hou.ui.postEventCallback(self.prepare_mode)
        else:
            assert hashlib.sha256(self.original.read_bytes()).hexdigest() == self.sha
            self.report.update(
                result="awaiting_visual_review",
                original_sha256_after=self.sha,
                verdict_file=str(self.path.with_suffix(".verdict.json")),
            )
            self.save()
            hou.ui.addEventLoopCallback(self.review)

    def review(self):
        verdict = self.path.with_suffix(".verdict.json")
        if verdict.is_file():
            value = json.loads(verdict.read_text(encoding="utf-8-sig"))
            if value.get("result") in ("passed", "failed"):
                self.report.update(
                    result=value["result"],
                    visual_verdict=value,
                    capture_acceptance=value["result"] == "passed",
                    product_acceptance=value.get("product_acceptance", False),
                )
                self.save()
                hou.ui.removeEventLoopCallback(self.review)
                if self.trace:
                    self.trace.uninstall_gt_trace()
                hou.exit(0 if value["result"] == "passed" else 1, suppress_save_prompt=True)

    def fail(self):
        self.report.update(result="failed", error=traceback.format_exc())
        self.save()
        if self.trace:
            self.trace.uninstall_gt_trace()
        hou.exit(1, suppress_save_prompt=True)


workflow = Benchmark()
