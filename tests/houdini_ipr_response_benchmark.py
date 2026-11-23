# SPDX-License-Identifier: Apache-2.0
"""IPR decision evidence: same owned capture/queue, worker versus test-only engine.

Run in a fresh Houdini GUI with a single render panel. The in-process reference
is confined to this test; no artist control or production backend selects it.
Measure first preview separately from full resolution, event-loop gaps while
edits are outstanding, and newest-scene recovery after bursts. No final-frame
throughput or Blender parity claim is made by this test.
"""

import json
import math
import os
from pathlib import Path
import runpy
import statistics
import threading
import time
import traceback

import hou
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry, queued


BACKEND = os.environ.get("CYCLES_HOUDINI_IPR_BENCH_BACKEND", "worker")
if BACKEND not in ("worker", "inprocess-reference"):
    raise ValueError("Unknown benchmark backend: " + BACKEND)
original_backend = queued.ExternalBackend


class InProcessReference:
    """Reproduce only the process boundary using the same controller and core."""

    def __init__(self, capture, desired, *, on_start=None):
        self.engine = ch._native.engine_create(capture)

    def poll(self):
        status = ch._native.status(self.engine)
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        return dict(
            success=True,
            error="",
            update_rejected=False,
            status=status,
            diagnostics=ch._native.diagnostics(self.engine),
            texture_inputs=ch._native.texture_inputs(self.engine),
            textures_changed=ch._native.textures_changed(self.engine),
            frame=ch._native.owned_frame(self.engine),
            renderer_pid=os.getpid(),
            transport={},
            process_memory="",
        )

    def apply(self, desired, acknowledged):
        ch._native.apply_snapshot(self.engine, desired, acknowledged)
        return self.poll()

    def control(self, command, value=None):
        if command == "preview":
            ch._native.set_preview_pass(self.engine, value)
        elif command == "pause":
            ch._native.pause(self.engine, value)
        elif command == "cancel":
            ch._native.cancel(self.engine)
        elif command == "memory":
            pass
        else:
            raise ValueError("Unexpected benchmark control: " + command)
        return self.poll()

    def close(self):
        ch._native.close(self.engine)
        return ch._native.status(self.engine)


if BACKEND == "inprocess-reference":
    queued.ExternalBackend = InProcessReference
os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "65536"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="response_fixture")[
    "HeavyResponse"
]


def distribution(values):
    ordered = sorted(values)
    return (
        dict(
            count=len(values),
            median_seconds=statistics.median(values),
            p95_seconds=ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
            maximum_seconds=max(values),
        )
        if values
        else dict(count=0)
    )


class ResponseDecision(HeavyResponse):
    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.adaptive = os.environ.get("CYCLES_HOUDINI_IPR_BENCH_ADAPTIVE", "0") == "1"
        if self.adaptive:
            self.report["adaptive_fixture"] = runpy.run_path(
                str(root / "examples/sop-fx-courtyard/adaptive_scene.py"), run_name="adaptive_fixture"
            )["upgrade"]()

    def __init__(self):
        super().__init__()
        self.report.update(
            benchmark_backend=BACKEND,
            adaptive_fixture_enabled=self.adaptive,
            repetitions=10,
            render_viewers=["cycles"],
            preview_policy="First actual scene preview, then full resolution; actual canvas generation observed",
            scope="Million-triangle native SOP scene. Includes host capture/cooking, queue, engine and panel polling. Timer gaps are event-loop proxies, not physical input latency. In-process backend is test-only.",
            trials=[],
            bursts=[],
            baseline_gaps=[],
            panel_polls=[],
            display_conversions=[],
        )
        self.main_thread = threading.get_ident()
        original_convert = self.panel._display_queue.convert

        def measured_convert(frame, settings):
            started = time.perf_counter()
            image = None
            try:
                image = original_convert(frame, dict(settings, collect_diagnostics=True))
                return image
            finally:
                self.report["display_conversions"].append(
                    dict(
                        seconds=time.perf_counter() - started,
                        thread=threading.get_ident(),
                        phase=self.report["phase"],
                        native=image.get("diagnostics") if image else None,
                    )
                )

        self.panel._display_queue.convert = measured_convert
        original_poll = self.panel.poll
        self.panel.timer.timeout.disconnect(original_poll)

        def measured_poll():
            started = time.perf_counter()
            try:
                return original_poll()
            finally:
                self.report["panel_polls"].append(
                    dict(seconds=time.perf_counter() - started, phase=self.report["phase"])
                )

        self.panel.poll = measured_poll
        self.panel.timer.timeout.connect(measured_poll)
        self.workloads = [
            ("camera", 10),
            ("transform", 10),
            ("surface-color", 10),
            ("mesh-deform", 3),
            ("mesh-topology", 3),
            ("noise-displacement", 3),
            ("curve-width", 3),
            ("point-radius", 3),
            ("volume-transform", 3),
            ("area-light", 3),
        ]
        if self.adaptive:
            self.workloads.append(("ground-noise", 3))
        self.trial_index = 0
        self.trial = None
        self.baseline_remaining = 2
        # Houdini idle callbacks can be delayed independently of rendering.
        # Observe actual Qt paint completion with a Qt timer, not host idleness.
        hou.ui.removeEventLoopCallback(self.callback)
        self.timer.timeout.connect(self.tick)
        self.timer.setInterval(10)
        self.applied_marker = 0
        self.painted = {}
        self.drag_window = None
        self.original_paint = type(self.panel.canvas).paintEvent

        def measured_paint(canvas, event):
            self.original_paint(canvas, event)
            if canvas is not self.panel.canvas or canvas.image.isNull():
                return
            metadata = getattr(canvas, "frame_metadata", {})
            if not metadata:
                return
            now = time.monotonic()
            generation = metadata["generation"]
            record = self.painted.setdefault(generation, dict(first=now, first_frame=dict(metadata)))
            if metadata["rendered_width"] == 960 and metadata["rendered_height"] == 540:
                record.setdefault("full", now)

        type(self.panel.canvas).paintEvent = measured_paint
        self.report["measurement"] = (
            "Qt paint completion for the exact submitted frame; 10 ms Qt observation timer. Earlier idle-callback timings are historical and must not be compared as equivalent."
        )

    def edit(self, name, repetition):
        if name == "ground-noise":
            hou.node("/mat/courtyard_ground_noise").parm("ground_relief").set(1.0 + 0.1 * (repetition % 2))
        else:
            super().edit(name, repetition)

    def read_frame(self, entry):
        status = entry.session.status()
        frame = entry.session.frame_info()
        if entry.error or status["state"] == "failed":
            raise RuntimeError(entry.error or status["error"])
        return status, frame

    def current_canvas(self, status, frame):
        return bool(
            frame
            and frame["generation"] == status["generation"]
            and self.panel.canvas.generation == frame["generation"]
            and frame["generation"] in self.painted
        )

    def start_trial(self, entry):
        trials = [(name, repeat) for name, count in self.workloads for repeat in range(count)]
        if self.trial_index >= len(trials):
            if entry.camera_refinement_pending or entry.change_serial > entry.applied_serial:
                return
            self.report["phase"] = "camera-burst"
            self.stage = "burst"
            self.burst_name = "camera"
            self.burst_index = 0
            self.burst_deadline = time.monotonic()
            self.burst_start = self.burst_deadline
            self.burst_images = set()
            self.burst_inputs = []
            self.burst_drag_images = set()
            self.burst_status = entry.session.status()
            self.release_at = None
            self.drag_window = hou.qt.floatingPanelWindow(self.scene.floatingPanel())
            self.drag_position = QtCore.QPoint(self.drag_window.width() // 2, self.drag_window.height() // 2)
            QtTest.QTest.mousePress(self.drag_window, QtCore.Qt.MiddleButton, QtCore.Qt.AltModifier, self.drag_position)
            assert QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton
            return
        name, repeat = trials[self.trial_index]
        self.report["phase"] = name + "-" + str(repeat)
        self.trial = dict(
            name=name,
            repetition=repeat,
            started=time.monotonic(),
            previous=entry.session.status(),
            apply_index=len(self.report["applies"]),
            heartbeat_index=len(self.report["heartbeat_gaps"]),
        )
        self.edit(name, repeat)
        self.trial["input_seconds"] = time.monotonic() - self.trial["started"]
        self.stage = "trial"
        self.save()

    def finish_trial(self, entry, status, frame):
        trial = self.trial
        trial.update(
            full_canvas_seconds=self.painted[status["generation"]]["full"] - trial["started"],
            status=status,
            geometry_conversion_delta=status["geometry_conversions"] - trial["previous"]["geometry_conversions"],
            extraction_delta=status["extractions"] - trial["previous"]["extractions"],
            host_apply_seconds=[v["host_seconds"] for v in self.report["applies"][trial["apply_index"] :]],
            heartbeat=distribution([v["seconds"] for v in self.report["heartbeat_gaps"][trial["heartbeat_index"] :]]),
            diagnostics=entry.session.diagnostics(),
        )
        if trial["name"] in ("camera", "transform", "surface-color"):
            assert trial["geometry_conversion_delta"] == 0, trial
        if trial["name"] in ("camera", "transform"):
            uploads = trial["diagnostics"].get("buffer_copy_requests", {})
            assert not set(uploads) & {"tri_verts", "tri_vindex", "curve_keys", "points"}, uploads
        assert trial["diagnostics"]["session_cancel_calls"] == 0, trial
        trial.pop("started")
        trial.pop("previous")
        trial.pop("apply_index")
        trial.pop("heartbeat_index")
        self.report["trials"].append(trial)
        self.trial_index += 1
        self.stage = "trial-rest"
        self.next_at = time.monotonic() + 0.08
        self.save()

    def burst(self, entry):
        now = time.monotonic()
        status, frame = self.read_frame(entry)
        if self.burst_name == "camera" and self.release_at is None:
            assert status["subdivision_tessellations"] == self.burst_status["subdivision_tessellations"], status
        if self.panel.canvas.generation > self.burst_status["generation"]:
            self.burst_images.add(self.panel.canvas.generation)
            if self.burst_index < 30:
                self.burst_drag_images.add(self.panel.canvas.generation)
        if self.burst_index < 30 and now >= self.burst_deadline:
            began = time.monotonic()
            previous = status["generation"]
            # End at a new camera position; alternating two old positions can
            # legitimately require no settled tessellation at all.
            if self.burst_name == "camera":
                self.camera.parm("tx").set(self.camera_x + 0.2 + 0.005 * self.burst_index)
            else:
                self.edit(self.burst_name, self.burst_index)
            # A real UI control must stay usable while capture/render is queued.
            if self.burst_index % 10 == 0:
                self.panel.exposure.setValue(0.02 * (self.burst_index // 10 % 2))
            self.burst_inputs.append(
                dict(
                    index=self.burst_index,
                    elapsed=began - self.burst_start,
                    input_seconds=time.monotonic() - began,
                    prior_generation=previous,
                    earlier_image_pending=not self.current_canvas(status, frame),
                )
            )
            self.burst_index += 1
            self.burst_deadline = began + 0.05
        if (
            self.burst_index == 30
            and self.current_canvas(status, frame)
            and not status.get("pending_update")
            and entry.change_serial == entry.applied_serial
        ):
            if self.burst_name == "camera":
                if self.release_at is None:
                    self.drag_feedback = now - (self.burst_deadline - 0.05)
                    QtTest.QTest.mouseRelease(
                        self.drag_window, QtCore.Qt.MiddleButton, QtCore.Qt.AltModifier, self.drag_position
                    )
                    assert QtWidgets.QApplication.mouseButtons() == QtCore.Qt.NoButton
                    self.release_at = now
                    return
                if entry.camera_refinement_pending or entry.change_serial > entry.applied_serial:
                    return
            record = dict(
                name=self.burst_name,
                inputs=self.burst_inputs,
                displayed_generations=sorted(self.burst_images),
                displayed_during_inputs=sorted(self.burst_drag_images),
                elapsed_seconds=now - self.burst_start,
                final_status=status,
                convergence_after_last_input_seconds=now - (self.burst_deadline - 0.05),
                coalesced_delta=status.get("coalesced_updates", 0) - self.burst_status.get("coalesced_updates", 0),
            )
            if self.burst_name == "camera":
                record.update(
                    drag_feedback_after_last_input_seconds=self.drag_feedback,
                    post_release_seconds=now - self.release_at,
                    subdivision_tessellation_delta=status["subdivision_tessellations"]
                    - self.burst_status["subdivision_tessellations"],
                )
                if self.adaptive:
                    assert record["subdivision_tessellation_delta"] == 1, record
            assert sum(v["earlier_image_pending"] for v in self.burst_inputs) > 0, record
            assert status["geometry_conversions"] == self.burst_status["geometry_conversions"], record
            assert self.burst_images, record
            if self.burst_name == "camera":
                assert self.burst_drag_images, "No new camera preview was painted while inputs continued"
            self.report["bursts"].append(record)
            self.save()
            if self.burst_name == "camera":
                self.burst_name = "transform"
                self.report["phase"] = "transform-burst"
                self.burst_index = 0
                self.burst_start = self.burst_deadline = now + 0.2
                self.burst_images = set()
                self.burst_drag_images = set()
                self.burst_inputs = []
                self.burst_status = status
            else:
                self.panel.cancel()
                self.stage = "stopping"
                self.report["phase"] = "stop"

    def finish_decision(self, entry):
        self.report["distributions"] = {
            name: dict(
                first_canvas=distribution(
                    [v["first_canvas_seconds"] for v in self.report["trials"] if v["name"] == name]
                ),
                full_canvas=distribution(
                    [v["full_canvas_seconds"] for v in self.report["trials"] if v["name"] == name]
                ),
                host_apply=distribution(
                    [s for v in self.report["trials"] if v["name"] == name for s in v["host_apply_seconds"]]
                ),
            )
            for name, _ in self.workloads
        }
        self.report["steady_heartbeat"] = distribution(
            [v["seconds"] for v in self.report["heartbeat_gaps"] if v["phase"] == "steady-baseline"]
        )
        self.report["stopped_heartbeat"] = distribution(
            [v["seconds"] for v in self.report["heartbeat_gaps"] if v["phase"] == "stopped-baseline"]
        )
        self.report["burst_heartbeat"] = distribution(
            [v["seconds"] for v in self.report["heartbeat_gaps"] if v["phase"].endswith("-burst")]
        )
        self.report["panel_poll_cost"] = distribution([v["seconds"] for v in self.report["panel_polls"]])
        self.report["display_conversion_cost"] = distribution(
            [v["seconds"] for v in self.report["display_conversions"]]
        )
        assert all(v["thread"] != self.main_thread for v in self.report["display_conversions"])
        diagnostics = entry.session.diagnostics()
        assert diagnostics["session_cancel_calls"] == 1, diagnostics
        self.report["final_diagnostics"] = diagnostics
        self.timer.stop()
        type(self.panel.canvas).paintEvent = self.original_paint
        registry.Entry.apply = self.original_apply
        registry.session_from_configuration = self.original_factory
        self.panel.dispose()
        registry.close_all()
        queued.ExternalBackend = original_backend
        self.panel._display_queue.close(wait=True)
        closed = entry.session.status()
        assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
        self.report.update(result="passed", closed=closed, wall_seconds=time.monotonic() - self.began)
        self.save()
        self.tab.close()
        self.scene.close()
        hou.ui.postEventCallback(lambda: hou.exit(0, suppress_save_prompt=True))

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        entry = registry.get(self.key)
        try:
            if time.monotonic() - self.began > 220:
                raise TimeoutError("IPR decision benchmark stalled")
            if entry is None:
                return
            now = time.monotonic()
            status, frame = self.read_frame(entry)
            if self.stage == "initial" and self.current_canvas(status, frame):
                self.report["renderer_pid"] = status.get("renderer_pid")
                assert (status["renderer_pid"] == os.getpid()) == (BACKEND == "inprocess-reference")
                self.point_code = self.obj.node("SPARK_PARTICLES").parm("snippet").eval()
                self.stage = "baseline"
                self.report["phase"] = "steady-baseline"
                self.next_at = now + 1.5
            elif self.stage == "baseline" and now >= self.next_at:
                self.start_trial(entry)
            elif (
                self.stage == "trial"
                and status["generation"] > self.trial["previous"]["generation"]
                and self.current_canvas(status, frame)
            ):
                if "first_canvas_seconds" not in self.trial:
                    paint = self.painted[status["generation"]]
                    self.trial.update(
                        first_canvas_seconds=paint["first"] - self.trial["started"],
                        first_frame=paint["first_frame"],
                        pending_on_first_image=bool(status.get("pending_update")),
                    )
                if "full" in self.painted[status["generation"]]:
                    self.finish_trial(entry, status, frame)
            elif self.stage == "trial-rest" and now >= self.next_at:
                if (
                    entry.camera_refinement_pending
                    or entry.change_serial > entry.applied_serial
                    or not self.current_canvas(status, frame)
                ):
                    return
                self.start_trial(entry)
            elif self.stage == "burst":
                self.burst(entry)
            elif self.stage == "stopping" and status["state"] == "cancelled":
                self.stage = "stopped-baseline"
                self.report["phase"] = "stopped-baseline"
                self.next_at = now + 1.5
            elif self.stage == "stopped-baseline" and now >= self.next_at:
                self.finish_decision(entry)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc(), phase=self.stage)
            self.save()
            self.timer.stop()
            type(self.panel.canvas).paintEvent = self.original_paint
            if self.drag_window:
                QtTest.QTest.mouseRelease(
                    self.drag_window, QtCore.Qt.MiddleButton, QtCore.Qt.AltModifier, self.drag_position
                )
            registry.Entry.apply = self.original_apply
            registry.session_from_configuration = self.original_factory
            registry.close_all()
            queued.ExternalBackend = original_backend
            hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
        finally:
            self.tick_busy = False


workflow = ResponseDecision()
