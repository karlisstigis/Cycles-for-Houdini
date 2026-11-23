# SPDX-License-Identifier: Apache-2.0
"""Trace camera or packed-object previews from node notification to actual paint."""

from support.paths import artifacts_root

import hashlib
import os
from pathlib import Path
import runpy
import time
import hou
from PySide6 import QtCore
from cycles_houdini import _native, native_scene, queued, registry
from cycles_houdini.display_queue import DisplayQueue

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_NO_MOTION"] = "1"
os.environ.setdefault("CYCLES_HOUDINI_BENCH_PACKED_COUNT", "1000")
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
fixture = runpy.run_path(str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="delivery_fixture")
PackedResponse = fixture["PackedResponse"]
HeavyResponse = fixture["HeavyResponse"]


class DeliveryWorkflow(PackedResponse):
    def __init__(self):
        self.target_width = int(os.environ.get("CYCLES_HOUDINI_DELIVERY_WIDTH", "960"))
        self.target_height = int(os.environ.get("CYCLES_HOUDINI_DELIVERY_HEIGHT", "540"))
        self.camera_edits = int(os.environ.get("CYCLES_HOUDINI_DELIVERY_EDITS", "12"))
        self.converter_mode = os.environ.get("CYCLES_HOUDINI_DELIVERY_CONVERTER", "auto")
        self.notifications = os.environ.get("CYCLES_HOUDINI_DELIVERY_NOTIFICATIONS", "1") != "0"
        self.edit_kind = os.environ.get("CYCLES_HOUDINI_DELIVERY_KIND", "camera")
        assert self.edit_kind in ("camera", "transform")
        assert self.converter_mode in ("auto", "cpu")
        self.frames = {}
        self.requests = {}
        self.host_applies = {}
        self.last_capture = None
        super().__init__()
        self.report["settings"].update(width=self.target_width, height=self.target_height)
        self.report["delivery_converter_mode"] = self.converter_mode
        self.report["delivery_notifications"] = self.notifications
        self.report["delivery_kind"] = self.edit_kind
        self.measurement_actor = self.camera if self.edit_kind == "camera" else self.packed_obj
        self.node_notifications = {}

        def notified(**kwargs):
            if self.case and self.case.get("phase") == self.report.get("phase"):
                parm = kwargs.get("parm_tuple")
                self.node_notifications.setdefault(self.case["phase"], []).append(
                    dict(at=time.monotonic(), parm=parm.name() if parm is not None else None)
                )

        self.node_observer = notified
        self.measurement_actor.addEventCallback((hou.nodeEventType.ParmTupleChanged,), self.node_observer)
        self.fixture_files = [
            artifacts_root() / "examples/sop-fx-courtyard" / name
            for name in ("sop-fx-courtyard-current.hiplc", "showcase.ocio")
        ]
        self.report["fixture_assets"] = [
            dict(
                path=p.relative_to(root).as_posix(),
                sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                bytes=p.stat().st_size,
            )
            for p in self.fixture_files
        ]
        if not self.notifications:
            # Exercise the production polling fallback on this same build.
            # This is a benchmark control, never an artist execution mode.
            self.panel._frame_wakeup.close()
        self.report["scope"] = (
            "One real viewport and one Cycles viewer. All host timestamps use Python monotonic time; worker-relative publication times are not subtracted from host timestamps. Tracing retains metadata only."
        )
        self.capture_original = native_scene.capture
        self.apply_saved = registry.Entry.apply
        self.decode_original = _native.decode_reply
        self.accept_original = queued.QueuedController.accept
        self.request_original = DisplayQueue.request
        self.take_original = DisplayQueue.take
        self.convert_original = self.panel._display_queue.convert
        canvas_type = type(self.panel.canvas)
        self.paint_saved = canvas_type.paintEvent
        self.merge_original = canvas_type.merge_frame

        def capture(*args, **kwargs):
            start = time.monotonic()
            result = self.capture_original(*args, **kwargs)
            self.last_capture = dict(
                start=start, end=time.monotonic(), payload=id(result), camera_only=kwargs.get("camera_only", False)
            )
            return result

        def apply(entry, *args, **kwargs):
            start = time.monotonic()
            captured = self.last_capture
            generation = self.apply_saved(entry, *args, **kwargs)
            if entry.key == self.key and generation is not None:
                self.host_applies[generation] = dict(
                    start=start,
                    end=time.monotonic(),
                    phase=self.report["phase"],
                    pose_preview=bool(kwargs.get("pose_preview")),
                    capture=dict(captured) if captured and captured["payload"] == id(args[0]) else None,
                )
            return generation

        def decode(*args):
            start = time.monotonic()
            reply = self.decode_original(*args)
            end = time.monotonic()
            if reply.get("frame") is not None:
                self.trace(reply["frame"]).update(
                    decoded_start=start, decoded_end=end, packet_bytes=memoryview(args[0]).nbytes
                )
            return reply

        def accept(controller, reply, submission=None):
            start = time.monotonic()
            if reply.get("frame") is not None:
                self.trace(reply["frame"]).setdefault("accept_begin", start)
            result = self.accept_original(controller, reply, submission)
            with controller.condition:
                if reply.get("frame") is not None and controller.image is reply["frame"]:
                    self.trace(reply["frame"]).update(
                        accepted=True,
                        generation=controller.image_generation,
                        controller_sequence=controller.image_sequence,
                        accept_end=time.monotonic(),
                    )
            return result

        def request(queue, token, frame, generation, sequence, settings):
            if queue is self.panel._display_queue:
                trace = self.trace(frame)
                trace.setdefault("requested", time.monotonic())
                trace.update(generation=generation, controller_sequence=sequence)
                self.requests[trace["revision"], sequence] = trace
            return self.request_original(queue, token, frame, generation, sequence, settings)

        def convert(frame, settings):
            trace = self.trace(frame)
            trace.setdefault("conversion_start", time.monotonic())
            method = _native.display_image if self.converter_mode == "cpu" else self.convert_original
            result = method(frame, dict(settings, collect_diagnostics=True))
            trace["conversion_end"] = time.monotonic()
            if result is not None:
                trace["conversion_diagnostics"] = dict(result.get("diagnostics", {}))
            return result

        def take(queue):
            result = self.take_original(queue)
            if queue is self.panel._display_queue and result and result[1] is not None:
                image = result[1]
                trace = self.image_trace(image)
                if trace is not None:
                    trace.setdefault("taken", time.monotonic())
            return result

        def merge(image, frame):
            start = time.monotonic()
            result = self.merge_original(image, frame)
            trace = self.image_trace(frame)
            if trace is not None:
                trace.update(merge_start=start, merge_end=time.monotonic())
            return result

        def paint(canvas, event):
            start = time.monotonic()
            self.paint_saved(canvas, event)
            if canvas is self.panel.canvas:
                trace = self.image_trace(getattr(canvas, "frame_metadata", {}))
                if trace is not None:
                    trace.setdefault("paint_start", start)
                    trace.setdefault("painted", time.monotonic())

        native_scene.capture = capture
        registry.Entry.apply = apply
        _native.decode_reply = decode
        queued.QueuedController.accept = accept
        DisplayQueue.request = request
        DisplayQueue.take = take
        self.panel._display_queue.convert = convert
        canvas_type.merge_frame = staticmethod(merge)
        canvas_type.paintEvent = paint

    def trace(self, frame):
        info = _native.frame_info(frame)
        key = info["generation"], info["sequence"]
        return self.frames.setdefault(
            key,
            dict(
                revision=key[0],
                native_sequence=key[1],
                rendered_size=[info["rendered_width"], info["rendered_height"]],
                pixel_bytes=info["pixel_bytes"],
                plane_storage=dict(info["plane_storage"]),
                logical_size=[info["width"], info["height"]],
            ),
        )

    def image_trace(self, image):
        return self.requests.get((image.get("render_revision", image.get("generation")), image.get("sequence")))

    def configure_scene(self):
        super().configure_scene()
        self.rop.setParms(
            dict(resx=self.target_width, resy=self.target_height, resolution_percent=100, ipr_resolution_percent=0)
        )

    def ready(self, entry):
        if entry.error:
            raise RuntimeError(entry.error)
        status = entry.session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        frame = entry.session.frame_info()
        metadata = getattr(self.panel.canvas, "frame_metadata", {})
        return bool(
            frame
            and frame["generation"] == status["generation"]
            and frame.get("current_scene")
            and not status.get("pending_update")
            and not status.get("capture_pending")
            and metadata.get("generation") == status["generation"]
            and metadata.get("render_revision") == status["render_revision"]
            and [metadata.get("rendered_width"), metadata.get("rendered_height")]
            == [self.target_width, self.target_height]
            and status["generation"] > self.before
        )

    def begin_edit(self, entry):
        if self.index == self.camera_edits:
            self.finish()
            return
        self.report["phase"] = self.edit_kind + "-" + str(self.index)
        self.before = entry.session.status()["generation"]
        self.before_status = entry.session.status()
        self.edit_started = time.monotonic()
        self.case = dict(
            name=self.edit_kind,
            repetition=self.index,
            phase=self.report["phase"],
            input_at=self.edit_started,
            before_revision=self.before_status.get("render_revision"),
        )
        if self.edit_kind == "camera":
            self.camera.parm("tx").set(self.camera_x + 0.03 * (self.index + 1))
        else:
            self.packed_obj.parm("tx").set(0.03 * (self.index + 1))
        self.case["parameter_action_seconds"] = time.monotonic() - self.edit_started
        current_case = self.case
        QtCore.QTimer.singleShot(0, lambda: current_case.update(ui_ready_at=time.monotonic()))
        self.stage = "editing"
        self.save()

    def finish(self):
        records = []
        for case in self.report["edits"]:
            generations = {g for g, h in self.host_applies.items() if h["phase"] == case["phase"]}
            candidates = [
                dict(t) for t in list(self.frames.values()) if t.get("generation") in generations and "painted" in t
            ]
            first = min(candidates, key=lambda t: t["painted"])
            generation = first["generation"]
            host = self.host_applies[generation]
            capture = host["capture"]
            assert first["revision"] != case["before_revision"], "Retained old pixels cannot count as an edited preview"
            available = [
                dict(t)
                for t in list(self.frames.values())
                if t.get("accepted") and t["revision"] == first["revision"] and "decoded_end" in t
            ]
            earliest = min(available, key=lambda t: t["decoded_end"])
            assert capture and capture["camera_only"] == (self.edit_kind == "camera" or host["pose_preview"])
            events = self.node_notifications[case["phase"]]
            first_notification = min(e["at"] for e in events)
            assert case["input_at"] <= first_notification <= capture["start"]
            order = [
                "decoded_start",
                "decoded_end",
                "accept_begin",
                "requested",
                "conversion_start",
                "conversion_end",
                "taken",
                "merge_start",
                "merge_end",
                "paint_start",
                "painted",
            ]
            assert all(k in first for k in order), first
            assert all(first[a] <= first[b] for a, b in zip(order, order[1:])), first
            records.append(
                dict(
                    phase=case["phase"],
                    generation=generation,
                    frame=first,
                    host=host,
                    input_at=case["input_at"],
                    notification_seconds=capture["start"] - case["input_at"],
                    capture_seconds=capture["end"] - capture["start"],
                    enqueue_seconds=host["end"] - host["start"],
                    input_to_decoded_seconds=first["decoded_end"] - case["input_at"],
                    decode_seconds=first["decoded_end"] - first["decoded_start"],
                    decoded_to_accept_seconds=first["accept_begin"] - first["decoded_end"],
                    accepted_to_request_seconds=first["requested"] - first["accept_begin"],
                    conversion_queue_seconds=first["conversion_start"] - first["requested"],
                    conversion_seconds=first["conversion_end"] - first["conversion_start"],
                    converted_to_take_seconds=first["taken"] - first["conversion_end"],
                    gui_merge_seconds=first["merge_end"] - first["merge_start"],
                    merge_to_paint_seconds=first["painted"] - first["merge_end"],
                    paint_seconds=first["painted"] - first["paint_start"],
                    total_seconds=first["painted"] - case["input_at"],
                )
            )
            records[-1].update(
                earliest_frame=earliest,
                first_input_to_decoded_seconds=earliest["decoded_end"] - case["input_at"],
                first_decoded_to_paint_seconds=first["painted"] - earliest["decoded_end"],
                input_to_ui_tick_seconds=case["ui_ready_at"] - case["input_at"],
            )
            records[-1].update(
                node_notifications=events,
                first_node_notification_seconds=first_notification - case["input_at"],
                notification_to_capture_seconds=capture["start"] - first_notification,
            )
        self.report["delivery_records"] = records
        self.report["packed_fixture"] = dict(
            instances=self.instance_count, triangles_per_prototype=2 * (self.prototype_rows - 1) ** 2
        )
        self.report["all_frame_traces"] = [dict(t) for t in list(self.frames.values())]
        for source, entry in zip(self.fixture_files, self.report["fixture_assets"]):
            assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"], "Fixture changed during timing"
        self.measurement_actor.removeEventCallback((hou.nodeEventType.ParmTupleChanged,), self.node_observer)
        self.panel.grab().save(str(self.path.with_suffix(".panel.png")))
        native_scene.capture = self.capture_original
        registry.Entry.apply = self.apply_saved
        _native.decode_reply = self.decode_original
        queued.QueuedController.accept = self.accept_original
        DisplayQueue.request = self.request_original
        DisplayQueue.take = self.take_original
        self.panel._display_queue.convert = self.convert_original
        type(self.panel.canvas).merge_frame = staticmethod(self.merge_original)
        type(self.panel.canvas).paintEvent = self.original_paint
        HeavyResponse.finish(self)


workflow = DeliveryWorkflow()
