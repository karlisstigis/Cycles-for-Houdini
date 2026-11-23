# SPDX-License-Identifier: Apache-2.0
"""QA instrumentation around the unchanged real release response GUI fixture.

Trace timings include instrumentation overhead and are not comparative results.
"""

import json
import os
from pathlib import Path
import sys
import threading
import time

import cycles_houdini as ch
from cycles_houdini import queued, display_queue, gpu_display, panel, native_scene
from cycles_houdini.viewer import viewer_display

started = time.monotonic_ns()
events = []
event_lock = threading.Lock()
context = {}
expected_device = os.environ.get("CYCLES_HOUDINI_TRACE_EXPECTED_DEVICE", "CPU")
report_path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
trace_path = report_path.with_suffix(".trace.jsonl")
saved = 0


def emit(event, **values):
    now = time.monotonic_ns()
    record = dict(
        event=event,
        monotonic_ns=now,
        monotonic_seconds=now / 1e9,
        elapsed_seconds=(now - started) / 1e9,
        perf_counter_ns=time.perf_counter_ns(),
        thread=threading.current_thread().name,
        thread_id=threading.get_ident(),
        **context,
        **values,
    )
    with event_lock:
        events.append(record)


def metadata(frame):
    if frame is None:
        return None
    try:
        value = ch._native.frame_info(frame)
        return {
            key: value.get(key)
            for key in (
                "generation",
                "sequence",
                "samples",
                "published_seconds",
                "complete",
                "rendered_width",
                "rendered_height",
                "width",
                "height",
            )
        }
    except Exception as error:
        return dict(error=str(error))


def status_info(status):
    return {
        key: status.get(key)
        for key in (
            "generation",
            "render_revision",
            "current_sample",
            "elapsed_seconds",
            "state",
            "progress",
            "first_image_seconds",
            "renderer_pid",
            "render_device_ids",
        )
    }


def controller_info(controller):
    return dict(
        controller=id(controller),
        requested=controller.requested,
        committed=controller.committed,
        revision=controller.physical,
        generation=controller.image_generation,
        sequence=controller.image_sequence,
        frame_identity=controller.frame_identity,
        presented_revision=controller.presented_revision,
    )


def flush():
    global saved
    with event_lock:
        chunk = events[saved:]
        saved = len(events)
    with trace_path.open("a", encoding="utf-8") as output:
        for record in chunk:
            output.write(json.dumps(record, default=str) + "\n")


def wrap(cls, name, action):
    original = getattr(cls, name)
    setattr(cls, name, action(original))


def exchange_hook(original):
    def exchange(self, command, payload=b"", desired=None):
        emit(
            "worker.exchange_start",
            backend=id(self),
            command=command,
            request=self.request + 1,
            payload_bytes=len(payload),
        )
        result = original(self, command, payload, desired)
        emit(
            "worker.decoded_data_arrival",
            backend=id(self),
            command=command,
            request=self.request,
            status=status_info(result.get("status", {})),
            frame=metadata(result.get("frame")),
            transport=self.last_exchange_profile,
        )
        return result

    return exchange


def read_hook(original):
    def read(self, count):
        result = original(self, count)
        if count == queued._HEADER.size and result[:4] == b"CHWP":
            magic, version, command, request, size, name_length = queued._HEADER.unpack(result)
            emit(
                "worker.reply_header_arrival",
                backend=id(self),
                command=command,
                request=request,
                mapping_bytes=size,
                mapping_name_bytes=name_length,
            )
        return result

    return read


wrap(queued.ExternalBackend, "exchange", exchange_hook)
wrap(queued.ExternalBackend, "read_exact", read_hook)


def accept_hook(original):
    def accept(self, reply, submission=None):
        emit(
            "controller.accept_start",
            **controller_info(self),
            submission=submission,
            status=status_info(reply.get("status", {})),
            frame=metadata(reply.get("frame")),
        )
        result = original(self, reply, submission)
        emit(
            "controller.accept_end",
            **controller_info(self),
            submission=submission,
            status=status_info(self.cached.get("status", {})),
        )
        return result

    return accept


wrap(queued.QueuedController, "accept", accept_hook)
for method in ("submit", "submit_navigation", "control", "presented"):

    def make_hook(original, method=method):
        def invoke(self, *args, **kwargs):
            emit(
                "controller." + method + "_start",
                **controller_info(self),
                arguments=[str(value)[:300] for value in args],
                keywords=kwargs,
            )
            result = original(self, *args, **kwargs)
            emit("controller." + method + "_end", **controller_info(self), result=str(result)[:150])
            return result

        return invoke

    wrap(queued.QueuedController, method, make_hook)


def follow_hook(original):
    def follow(self, source, epoch, settings, describe):
        previous = self.source_config
        serial = self.source_serial
        result = original(self, source, epoch, settings, describe)
        differences = []
        if previous is not None:
            for name, old, new in zip(
                ("source", "epoch", "settings", "describe"), previous[1:], (source, epoch, settings, describe)
            ):
                if old != new:
                    differences.append(name)
        emit(
            "display.follow",
            queue=id(self),
            serial_before=serial,
            serial_after=self.source_serial,
            changed=serial != self.source_serial,
            differences=differences,
            epoch=epoch,
            settings=settings,
            source=str(source),
            describe=str(describe),
            token=self.token,
        )
        return result

    return follow


def request_hook(original):
    def request(self, token, frame, generation, sequence, settings):
        emit(
            "display.request",
            queue=id(self),
            token=token,
            generation=generation,
            revision=token[-1],
            sequence=sequence,
            frame=metadata(frame),
            settings=settings,
        )
        return original(self, token, frame, generation, sequence, settings)

    return request


def take_hook(original):
    def take(self):
        result = original(self)
        if result:
            token, image, error = result
            emit(
                "display.take",
                queue=id(self),
                token=token,
                current_token=self.token,
                image={
                    key: image.get(key)
                    for key in ("generation", "render_revision", "sequence", "samples", "published_seconds")
                }
                if image
                else None,
                error=error,
            )
        return result

    return take


def invalidate_hook(original):
    def invalidate(self, token):
        previous = self.token
        result = original(self, token)
        if previous != self.token:
            emit("display.invalidate", queue=id(self), previous_token=previous, token=self.token)
        return result

    return invalidate


wrap(display_queue.DisplayQueue, "follow", follow_hook)
wrap(display_queue.DisplayQueue, "request", request_hook)
wrap(display_queue.DisplayQueue, "take", take_hook)
wrap(display_queue.DisplayQueue, "invalidate", invalidate_hook)


def convert_hook(original):
    def convert(self, frame, settings):
        caller = sys._getframe(1).f_locals
        token = caller.get("token")
        sequence = caller.get("sequence")
        generation = caller.get("generation")
        queue = caller.get("self")
        emit(
            "display.convert_start",
            converter=id(self),
            queue=id(queue),
            token=token,
            generation=generation,
            sequence=sequence,
            frame=metadata(frame),
            settings=settings,
        )
        begun = time.monotonic_ns()
        try:
            result = original(self, frame, settings)
            emit(
                "display.convert_end",
                converter=id(self),
                queue=id(queue),
                token=token,
                generation=generation,
                sequence=sequence,
                duration_seconds=(time.monotonic_ns() - begun) / 1e9,
                image={key: result.get(key) for key in ("generation", "samples", "published_seconds")}
                if result
                else None,
                current_token=getattr(queue, "token", None),
            )
            return result
        except BaseException as error:
            emit("display.convert_error", token=token, sequence=sequence, error=str(error))
            raise

    return convert


wrap(gpu_display.Converter, "__call__", convert_hook)
painted = set()


def paint_hook(original):
    def paint(self, event):
        value = dict(getattr(self, "frame_metadata", {}))
        identity = (id(self), value.get("generation"), value.get("render_revision"), value.get("sequence"))
        first = identity not in painted and not self.image.isNull()
        if first:
            emit("canvas.paint_start", canvas=id(self), metadata=value)
        result = original(self, event)
        if first:
            painted.add(identity)
            emit("canvas.first_paint_end", canvas=id(self), metadata=value)
        return result

    return paint


wrap(panel.ImageCanvas, "paintEvent", paint_hook)
# The default viewer display draws with OpenGL; its canvas has the same hook.
wrap(viewer_display.gpu_canvas_class(), "paintEvent", paint_hook)


def measure_gui_call(name):
    original = getattr(panel.RenderPanel, name)

    def measured(self, *args, **kwargs):
        begin = time.monotonic_ns()
        try:
            return original(self, *args, **kwargs)
        finally:
            elapsed = (time.monotonic_ns() - begin) / 1e6
            if elapsed >= 5:
                emit("gui.long_call", method=name, start_ns=begin, milliseconds=elapsed)

    setattr(panel.RenderPanel, name, measured)


for gui_method in (
    "poll",
    "poll_memory",
    "refresh_color_policy",
    "present_available",
    "poll_texture_inputs",
    "update_render_controls",
    "_attach_active",
):
    measure_gui_call(gui_method)

configuration = native_scene.configuration


def configure(*args, **kwargs):
    result = configuration(*args, **kwargs)
    assert list(result[0]) == [expected_device], ("Unexpected effective device configuration", result)
    emit("scene.configuration", configuration=result)
    return result


native_scene.configuration = configure
fixture = Path(__file__).with_name("houdini_release_response_gui_smoke.py")
namespace = dict(__file__=str(fixture), __name__="camera_feedback_original_fixture")
text = fixture.read_text()
assert text.count("workflow = Workflow()") == 1
exec(compile(text.replace("workflow = Workflow()", ""), str(fixture), "exec"), namespace)
Workflow = namespace["Workflow"]
original_edit = Workflow.edit
original_save = Workflow.save


def edit(self, status):
    assert status["render_device_ids"] == [expected_device], ("Unexpected actual worker devices", status)
    kind, repetition = self.steps[self.index]
    context.update(edit_index=self.index, edit_kind=kind, edit_repetition=repetition)
    emit("fixture.edit_start", before=status_info(status))
    result = original_edit(self, status)
    emit(
        "fixture.edit_end",
        parameter_action_seconds=self.case["parameter_action_seconds"],
        input_at=self.case["input_at"],
    )
    return result


def save(self):
    self.report["instrumentation"] = dict(
        trace=str(trace_path),
        events=len(events),
        runtime=ch.__file__,
        native=ch._native.__file__,
        timing_scope="Instrumented diagnostic run only; not comparable latency measurements",
    )
    result = original_save(self)
    flush()
    return result


Workflow.edit = edit
Workflow.save = save
emit(
    "trace.start",
    runtime=ch.__file__,
    native=ch._native.__file__,
    fixture=str(fixture),
    fixture_sha256=__import__("hashlib").sha256(fixture.read_bytes()).hexdigest(),
)
workflow = Workflow()
