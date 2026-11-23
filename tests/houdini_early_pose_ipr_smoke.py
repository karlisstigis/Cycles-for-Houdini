# SPDX-License-Identifier: Apache-2.0
"""Artist OBJ edits paint before source capture/scan/conversion can finish."""

import os
from pathlib import Path
import runpy
import threading
import time
import traceback
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import _native, native_scene, registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ.setdefault("CYCLES_HOUDINI_BENCH_PACKED_COUNT", "1000")
os.environ["CYCLES_HOUDINI_BENCH_NO_MOTION"] = "1"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
fixture = runpy.run_path(str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="early_pose_fixture")
PackedResponse = fixture["PackedResponse"]
HeavyResponse = fixture["HeavyResponse"]


class EarlyPoseWorkflow(PackedResponse):
    def __init__(self):
        self.host_hold = False
        self.release = threading.Event()
        self.entered = threading.Event()
        self.round = 0
        self.cases = []
        self.full_captures = 0
        super().__init__()
        self.queue_original = self.panel.queue_render
        self.capture_original = native_scene.capture
        self.prewarm_original = _native.prewarm_apply
        self.convert_original = _native.convert_apply

        def queue(*args):
            if not self.host_hold:
                return self.queue_original(*args)

        def capture(*args, **kwargs):
            if not kwargs.get("camera_only", False):
                assert not self.host_hold, "Early pose preview attempted full source acquisition"
                self.full_captures += 1
            return self.capture_original(*args, **kwargs)

        self.panel.queue_render = queue
        native_scene.capture = capture
        self.report["scope"] = (
            "Actual Render/Auto/Pause/Stop controls and OBJ parameter notifications; a million-triangle shared prototype plus the mixed courtyard; exactly one Cycles viewer."
        )

    def restore_barrier(self):
        self.host_hold = False
        self.release.set()
        _native.prewarm_apply = self.prewarm_original
        _native.convert_apply = self.convert_original

    def begin_round(self, entry):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.base_status = entry.session.status()
        self.base_applied = entry.applied_serial
        self.base_full = self.full_captures
        if self.round == 0:
            self.host_hold = True
            self.deform.parm("rx").set(17)
            self.move(entry)
        else:
            release, entered = self.release, self.entered
            original = self.prewarm_original if self.round == 1 else self.convert_original

            def held(job):
                assert threading.current_thread() is not threading.main_thread()
                entered.set()
                if not release.wait(12):
                    raise TimeoutError("Object preview did not paint before geometry release")
                return original(job)

            if self.round == 1:
                _native.prewarm_apply = held
            else:
                _native.convert_apply = held
            self.deform.parm("rx").set(17 + 13 * self.round)
            self.stage = "waiting-barrier"

    def move(self, entry):
        self.host_hold = True
        self.pose_started = time.monotonic()
        self.before_pose_serial = entry.applied_serial
        self.packed_obj.parm("tx").set(0.2 + 0.17 * self.round)
        if self.round:
            self.camera.parm("tx").set(self.camera_x + 0.08 * self.round)
        self.stage = "preview"
        self.report["phase"] = ("before-capture", "scan", "conversion")[self.round]

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 125:
                raise TimeoutError(self.stage)
            entry = registry.get(self.key)
            if entry is None:
                return
            status = entry.session.status()
            if entry.error or status["state"] == "failed":
                raise RuntimeError(entry.error or status["error"])
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                self.pid = status["renderer_pid"]
                self.begin_round(entry)
            elif self.stage == "waiting-barrier":
                if self.entered.is_set():
                    self.move(entry)
            elif self.stage == "preview":
                frame = entry.session.frame_info()
                metadata = self.panel.canvas.frame_metadata
                generation = status["generation"]
                if not (
                    frame
                    and generation > self.base_status["generation"]
                    and frame.get("current_scene")
                    and frame["generation"] == generation
                    and self.painted.get(generation)
                    and metadata.get("generation") == generation
                    and metadata.get("render_revision") == status.get("render_revision")
                ):
                    return
                diagnostics = entry.session.diagnostics()
                events = diagnostics["controller"]["events"]
                # A camera preview may paint first while owned object poses are
                # being prepared. Wait for the actual moved-object image.
                if not any(
                    e["submission"] == generation
                    and e["accepted"]
                    and e.get("navigation_kind") == "objects"
                    and e["physical_generation"] == status["render_revision"]
                    for e in events
                ):
                    return
                assert status["render_revision"] != self.base_status["render_revision"], (
                    "Old retained pixels were relabelled"
                )
                assert self.host_hold and not self.release.is_set()
                assert entry.pose_preview_serial > self.before_pose_serial
                assert entry.applied_serial < entry.change_serial, "Pose preview acknowledged source edits"
                assert status["pending_update"], status
                assert status["renderer_pid"] == self.pid
                assert status["geometry_conversions"] == self.base_status["geometry_conversions"]
                if self.round == 0:
                    assert self.full_captures == self.base_full
                else:
                    assert self.entered.is_set() and status["capture_pending"]
                self.cases.append(
                    dict(
                        stage=self.report["phase"],
                        preview_seconds=time.monotonic() - self.pose_started,
                        preview_before_release=True,
                        pending_source_preserved=True,
                        status=status,
                        diagnostics=diagnostics,
                    )
                )
                self.panel.grab().save(str(self.path.with_name("early-pose-" + str(self.round) + ".png")))
                self.before = self.base_status["generation"]
                self.restore_barrier()
                self.stage = "refined"
            elif self.stage == "refined":
                if not self.ready(entry):
                    return
                assert status["renderer_pid"] == self.pid and not status["pending_update"]
                assert status["geometry_conversions"] > self.base_status["geometry_conversions"]
                assert entry.applied_serial == entry.change_serial
                self.cases[-1]["refined_status"] = status
                if self.round < 2:
                    self.round += 1
                    self.begin_round(entry)
                else:
                    self.panel.auto.setChecked(False)
                    self.before = status["generation"]
                    self.requests = entry.session._controller.requested
                    self.packed_obj.parm("tx").set(0.8)
                    self.next_at = time.monotonic() + 0.3
                    self.stage = "auto-off"
            elif self.stage == "auto-off":
                if time.monotonic() < self.next_at:
                    return
                assert entry.session._controller.requested == self.requests, "Auto-off submitted a pose preview"
                self.panel.auto.setChecked(True)
                self.stage = "auto-on"
            elif self.stage == "auto-on":
                if not self.ready(entry):
                    return
                QtTest.QTest.mouseClick(self.panel.pause_button, QtCore.Qt.LeftButton)
                self.before = status["generation"]
                self.requests = entry.session._controller.requested
                self.packed_obj.parm("tx").set(0.9)
                self.next_at = time.monotonic() + 0.3
                self.stage = "paused"
            elif self.stage == "paused":
                if time.monotonic() < self.next_at:
                    return
                assert entry.session._controller.requested == self.requests, "Pause submitted a pose preview"
                QtTest.QTest.mouseClick(self.panel.pause_button, QtCore.Qt.LeftButton)
                self.stage = "resumed"
            elif self.stage == "resumed":
                if not self.ready(entry):
                    return
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.requests = entry.session._controller.requested
                self.packed_obj.parm("tx").set(1.0)
                self.next_at = time.monotonic() + 0.3
                self.stage = "stopped"
            elif self.stage == "stopped":
                if time.monotonic() < self.next_at:
                    return
                assert entry.session._controller.requested == self.requests, "Stop submitted a pose preview"
                self.before = status["generation"]
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "restart"
            elif self.stage == "restart":
                if not self.ready(entry):
                    return
                assert status["renderer_pid"] == self.pid
                assert entry.session.diagnostics()["session_cancel_calls"] == 1
                self.report["early_pose_cases"] = self.cases
                self.report["lifecycle_checks"] = [
                    "Auto off/on",
                    "Pause/resume",
                    "Stop/restart",
                    "Persistent worker retained",
                    "Pending geometry committed",
                ]
                self.report["final_diagnostics"] = entry.session.diagnostics()
                self.restore_barrier()
                native_scene.capture = self.capture_original
                self.panel.queue_render = self.queue_original
                type(self.panel.canvas).paintEvent = self.original_paint
                HeavyResponse.finish(self)
        except Exception:
            self.restore_barrier()
            native_scene.capture = self.capture_original
            self.report.update(result="failed", error=traceback.format_exc(), early_pose_cases=self.cases)
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = EarlyPoseWorkflow()
