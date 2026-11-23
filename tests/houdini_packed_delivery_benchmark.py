# SPDX-License-Identifier: Apache-2.0
"""Host-clock delivery attribution on the actual heavy packed preview fixture.

Metadata-only tracing; no extra frame copies or worker/host clock subtraction.
Viewport controls are diagnostic test conditions, never renderer modes.
"""

import os
import threading
import time
import traceback
import weakref

import hou
from PySide6 import QtCore
from cycles_houdini import _native, native_scene, queued, registry
from cycles_houdini.display_queue import DisplayQueue
from cycles_houdini.viewer_wakeup import ViewerWakeup
from houdini_packed_preview_benchmark import Benchmark


class DeliveryBenchmark(Benchmark):
    def __init__(self):
        self.trace_lock = threading.RLock()
        self.native_frames, self.display_frames, self.conversions = {}, {}, {}
        self.host_applies, self.captures, self.host_spans, self.pulses = [], {}, [], []
        self.notifications, self.hooks = [], []
        self.panel_traced = False
        self.trace_enabled = True
        self.viewport_window = None
        self.viewport_setup_ready = False
        self.viewport_mode = os.environ.get("CYCLES_HOUDINI_DELIVERY_VIEWPORT", "existing")
        assert self.viewport_mode in ("existing", "single", "single-no-redraw")
        super().__init__()
        self.install_hooks()
        self.save()

    def prepare_viewports(self):
        self.viewport_window = hou.qt.floatingPanelWindow(self.scene.floatingPanel())
        if self.viewport_window is None:
            return False  # Houdini realizes floating windows after construction.
        views = [pane for pane in hou.ui.paneTabs() if pane.type() == hou.paneTabType.SceneViewer]
        self.report["delivery_viewports"] = dict(
            condition=self.viewport_mode,
            initial=[dict(name=pane.name(), measured=pane == self.scene) for pane in views],
        )
        if self.viewport_mode != "existing":
            for pane in views:
                if pane != self.scene:
                    pane.setType(hou.paneTabType.NetworkEditor)
        if self.viewport_mode == "single-no-redraw":
            self.viewport_window.setUpdatesEnabled(False)
        self.report["delivery_viewports"]["active_count"] = sum(
            pane.type() == hou.paneTabType.SceneViewer for pane in hou.ui.paneTabs()
        )
        self.report["delivery_viewports"]["floating_window_size"] = [
            self.viewport_window.width(),
            self.viewport_window.height(),
        ]
        return True

    def context(self):
        return dict(case=self.index, phase=self.phase, at=time.perf_counter())

    def hook(self, owner, name, wrapper, *, static=False):
        original = getattr(owner, name)
        self.hooks.append((owner, name, original, static))
        setattr(owner, name, staticmethod(wrapper(original)) if static else wrapper(original))

    def native_trace(self, frame):
        info = _native.frame_info(frame)
        key = info["generation"], info["sequence"]
        with self.trace_lock:
            record = self.native_frames.setdefault(
                key,
                dict(
                    revision=key[0],
                    native_sequence=key[1],
                    samples=info["samples"],
                    rendered_size=[info["rendered_width"], info["rendered_height"]],
                ),
            )
        return record

    def span(self, kind, start, **values):
        end = time.perf_counter()
        if self.trace_enabled and end - start >= 0.0002:
            with self.trace_lock:
                self.host_spans.append(dict(kind=kind, start=start, end=end, **self.context(), **values))

    def install_hooks(self):
        def capture(original):
            def wrapped(*args, **kwargs):
                start = time.perf_counter()
                result = original(*args, **kwargs)
                self.captures[id(result)] = dict(
                    start=start, end=time.perf_counter(), camera_only=bool(kwargs.get("camera_only")), **self.context()
                )
                return result

            return wrapped

        self.hook(native_scene, "capture", capture)

        def apply(original):
            def wrapped(entry, update, *args, **kwargs):
                start = time.perf_counter()
                capture_record = self.captures.pop(id(update), None)
                generation = original(entry, update, *args, **kwargs)
                if entry is getattr(self.panel, "entry", None) and generation is not None:
                    self.host_applies.append(
                        dict(
                            start=start,
                            end=time.perf_counter(),
                            generation=generation,
                            pose_preview=bool(kwargs.get("pose_preview")),
                            capture=capture_record,
                            **self.context(),
                        )
                    )
                return generation

            return wrapped

        self.hook(registry.Entry, "apply", apply)

        def pump(original):
            def wrapped(session, *args, **kwargs):
                start = time.perf_counter()
                try:
                    return original(session, *args, **kwargs)
                finally:
                    self.span("capture_pump", start)

            return wrapped

        self.hook(queued.WorkerSession, "_pump_capture", pump)

        def decode(original):
            def wrapped(*args, **kwargs):
                start = time.perf_counter()
                reply = original(*args, **kwargs)
                if reply.get("frame") is not None:
                    record = self.native_trace(reply["frame"])
                    with self.trace_lock:
                        record.setdefault("decoded_start", start)
                        record.setdefault("decoded_end", time.perf_counter())
                return reply

            return wrapped

        self.hook(_native, "decode_reply", decode)

        def accept(original):
            def wrapped(controller, reply, *args, **kwargs):
                record = self.native_trace(reply["frame"]) if reply.get("frame") is not None else None
                if record is not None:
                    with self.trace_lock:
                        record.setdefault("accept_begin", time.perf_counter())
                result = original(controller, reply, *args, **kwargs)
                if record is not None:
                    with controller.condition:
                        accepted = controller.image is reply["frame"]
                    with self.trace_lock:
                        record["accepted"] = record.get("accepted", False) or accepted
                        record.setdefault("accept_end", time.perf_counter())
                return result

            return wrapped

        self.hook(queued.QueuedController, "accept", accept)

        def request(original):
            def wrapped(queue, token, frame, generation, sequence, settings):
                if self.panel and queue is self.panel._display_queue:
                    native = self.native_trace(frame)
                    with self.trace_lock:
                        self.display_frames.setdefault(
                            (native["revision"], sequence),
                            dict(
                                revision=native["revision"],
                                native_sequence=native["native_sequence"],
                                generation=generation,
                                sequence=sequence,
                                requested=time.perf_counter(),
                            ),
                        )
                return original(queue, token, frame, generation, sequence, settings)

            return wrapped

        self.hook(DisplayQueue, "request", request)

        def take(original):
            def wrapped(queue):
                result = original(queue)
                if self.panel and queue is self.panel._display_queue and result and result[1] is not None:
                    image = result[1]
                    with self.trace_lock:
                        key = image.get("render_revision", image.get("generation")), image.get("sequence")
                        if key in self.display_frames:
                            self.display_frames[key].setdefault("taken", time.perf_counter())
                            self.display_frames[key]["conversion"] = self.conversions[image["_delivery_trace"]]
                return result

            return wrapped

        self.hook(DisplayQueue, "take", take)

        for method in ("notify", "consume"):

            def pulse(original, method=method):
                def wrapped(wakeup, *args, **kwargs):
                    if self.panel and wakeup is self.panel._frame_wakeup:
                        with self.trace_lock:
                            self.pulses.append(dict(kind=method, **self.context()))
                    return original(wakeup, *args, **kwargs)

                return wrapped

            self.hook(ViewerWakeup, method, pulse)

        for actor in (self.f.camera, self.f.obj):

            def observed(actor=actor, **kwargs):
                self.notifications.append(dict(source=actor.path(), **self.context()))

            actor.addEventCallback((hou.nodeEventType.ParmTupleChanged,), observed)
            self.hooks.append((actor, None, observed, False))

    def install_panel_hooks(self):
        self.panel_traced = True
        queue = self.panel._display_queue
        original_poll = self.panel.poll

        def convert(original):
            def wrapped(frame, settings):
                native = self.native_trace(frame)
                start = time.perf_counter()
                image = original(frame, dict(settings, collect_diagnostics=True))
                end = time.perf_counter()
                if image is not None:
                    with self.trace_lock:
                        serial = len(self.conversions)
                        self.conversions[serial] = dict(
                            start=start,
                            end=end,
                            revision=native["revision"],
                            native_sequence=native["native_sequence"],
                            diagnostics=image.get("diagnostics", {}),
                        )
                    image["_delivery_trace"] = serial
                return image

            return wrapped

        self.hook(queue, "convert", convert)

        def ready(original):
            def wrapped():
                with self.trace_lock:
                    self.pulses.append(dict(kind="queue_notify", **self.context()))
                return original()

            return wrapped

        self.hook(queue, "on_ready", ready)

        def merge(original):
            def wrapped(image, frame):
                start = time.perf_counter()
                result = original(image, frame)
                with self.trace_lock:
                    record = self.display_frames.get(
                        (frame.get("render_revision", frame.get("generation")), frame.get("sequence"))
                    )
                    if record is not None:
                        record.setdefault("merge_start", start)
                        record.setdefault("merge_end", time.perf_counter())
                return result

            return wrapped

        self.hook(type(self.panel.canvas), "merge_frame", merge, static=True)

        def paint(original):
            def wrapped(canvas, event):
                start = time.perf_counter()
                try:
                    result = original(canvas, event)
                    if canvas is self.panel.canvas:
                        # Empty widgets can paint before the first image has
                        # initialized metadata. That is a normal Qt lifecycle.
                        metadata = getattr(canvas, "frame_metadata", {})
                        with self.trace_lock:
                            record = self.display_frames.get(
                                (metadata.get("render_revision"), metadata.get("sequence"))
                            )
                            if record is not None:
                                record.setdefault("paint_start", start)
                                record.setdefault("painted", time.perf_counter())
                    return result
                except Exception:
                    self.error = traceback.format_exc()

            return wrapped

        self.hook(type(self.panel.canvas), "paintEvent", paint)

        for method in ("poll", "present_available", "_run_queued_render", "_run_queued_camera"):

            def timed(original, method=method):
                def wrapped(panel, *args, **kwargs):
                    start = time.perf_counter()
                    try:
                        return original(panel, *args, **kwargs)
                    finally:
                        self.span(method, start)

                return wrapped

            self.hook(type(self.panel), method, timed)
        # These callbacks were bound during widget construction, before its
        # class hooks. Rebind them to the instrumented methods exactly once.
        self.panel.timer.timeout.disconnect(original_poll)
        self.panel.timer.timeout.connect(self.panel.poll)
        wakeup = self.panel._frame_wakeup
        with wakeup.lock:
            self.hooks.append((wakeup, "callback", wakeup.callback, False))
            wakeup.callback = weakref.WeakMethod(self.panel.present_available)

    def save(self):
        start = time.perf_counter()
        super().save()
        self.span("report_write", start)

    def tick(self):
        if not self.viewport_setup_ready:
            self.viewport_setup_ready = self.prepare_viewports()
            if not self.viewport_setup_ready:
                if time.perf_counter() - self.started > 15:
                    self.finish("Houdini did not realize the floating viewport window")
                return
        super().tick()
        if not self.finished and self.panel is not None and not self.panel_traced:
            self.install_panel_hooks()

    def begin_action(self, status):
        super().begin_action(status)
        self.case["input_at"] = self.action_started
        case = self.case
        QtCore.QTimer.singleShot(0, lambda: case.setdefault("next_ui_turn_at", time.perf_counter()))

    def finish(self, error=None):
        if self.finished:
            return
        try:
            if error is None:
                records = []
                with self.trace_lock:
                    for case in self.report["cases"]:
                        metadata = case["first_paint"]
                        displayed = dict(self.display_frames[metadata["render_revision"], metadata["sequence"]])
                        native = dict(self.native_frames[displayed["revision"], displayed["native_sequence"]])
                        assert displayed["revision"] > case["before_render_revision"]
                        assert native["accepted"]
                        order = [
                            native["decoded_start"],
                            native["decoded_end"],
                            native["accept_begin"],
                            displayed["requested"],
                            displayed["conversion"]["start"],
                            displayed["conversion"]["end"],
                            displayed["taken"],
                            displayed["merge_start"],
                            displayed["merge_end"],
                            displayed["paint_start"],
                            displayed["painted"],
                        ]
                        assert all(a <= b for a, b in zip(order, order[1:])), order
                        records.append(
                            dict(
                                kind=case["kind"],
                                repetition=case["repetition"],
                                input_at=case["input_at"],
                                first_frame=native,
                                displayed=displayed,
                                total_seconds=displayed["painted"] - case["input_at"],
                                input_to_decoded_seconds=native["decoded_end"] - case["input_at"],
                                decoded_to_paint_seconds=displayed["painted"] - native["decoded_end"],
                                converted_to_take_seconds=displayed["taken"] - displayed["conversion"]["end"],
                                next_ui_turn_seconds=case["next_ui_turn_at"] - case["input_at"],
                            )
                        )
                    self.report["delivery_records"] = records
                    self.report["delivery_native_frames"] = list(self.native_frames.values())
                    self.report["delivery_display_frames"] = list(self.display_frames.values())
                    self.report["delivery_host_spans"] = list(self.host_spans)
                    self.report["delivery_host_applies"] = list(self.host_applies)
                    self.report["delivery_notifications"] = list(self.notifications)
                    self.report["delivery_pulses"] = list(self.pulses)
        except Exception:
            error = traceback.format_exc()
        finally:
            self.trace_enabled = False
            for owner, name, original, static in reversed(self.hooks):
                try:
                    if name is None:
                        owner.removeEventCallback((hou.nodeEventType.ParmTupleChanged,), original)
                    else:
                        setattr(owner, name, staticmethod(original) if static else original)
                except Exception:
                    self.report.setdefault("trace_cleanup_errors", []).append(traceback.format_exc())
                    error = error or "Delivery tracer restoration failed"
            if self.viewport_window is not None:
                self.viewport_window.setUpdatesEnabled(True)
        super().finish(error)


if __name__ == "__main__":
    benchmark = DeliveryBenchmark()
