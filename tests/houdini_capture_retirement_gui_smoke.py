# SPDX-License-Identifier: Apache-2.0
"""100k-instance viewer Stop/restart and close remain usable during held cleanup."""

import json
import os
from pathlib import Path
import threading
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from packed_deformation_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.f
        f.nested.parm("viewportlod").set("box")
        f.motion.parm("test_rows").set(721)
        f.targets.setParms(dict(rows=1000, cols=100))
        f.targets.parmTuple("size").set((4, 2.25))
        f.grid.parmTuple("size").set((0.004, 0.001))
        f.width, f.height = 320, 180
        self.rop = f.rop(f.nested, interactive_samples=4)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 600))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.session = self.target_handle = None
        self.original_convert = ch._native.convert_apply
        self.original_discard = ch._native.discard_apply
        self.original_retire = ch._native.retire_capture
        self.original_close = ch._native.close
        self.barriers = []
        self.close_times = []
        self.discards = []

        def timed_close(handle):
            began = time.perf_counter()
            result = self.original_close(handle)
            if handle is self.target_handle:
                self.close_times.append(
                    dict(
                        seconds=time.perf_counter() - began,
                        phase=self.stage,
                        on_host_thread=threading.current_thread() is threading.main_thread(),
                    )
                )
            return result

        ch._native.close = timed_close
        self.started = self.last = time.perf_counter()
        self.stage = "mount"
        self.busy = self.finished = False
        self.report = dict(
            result="running",
            scope=__doc__ + " Deterministic barriers prove scheduling, not a universal timing budget.",
            instances=100000,
            prototype_triangles=1036800,
            device=f.device,
            heartbeats=[],
            pending_edits=[],
        )
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def save(self):
        self.report["phase"] = self.stage
        self.path.write_text(json.dumps(self.report, indent=2))

    def barrier(self):
        entered, release = threading.Event(), threading.Event()
        self.barriers.append(release)
        return entered, release

    def hold_conversion(self):
        self.convert_entered, self.convert_release = self.barrier()

        def held(job):
            result = self.original_convert(job)
            self.convert_entered.set()
            assert self.convert_release.wait(8), "Host failed to release converted payload"
            return result

        ch._native.convert_apply = held

    def submit(self):
        began = time.perf_counter()
        generation = self.panel.entry.apply(native_scene.capture(self.rop), hou.time())
        self.report["pending_edits"].append(
            dict(seconds=time.perf_counter() - began, generation=generation, phase=self.stage)
        )
        return generation

    def ready(self, status):
        assert status["state"] != "failed" and not status["error"], status
        frame = self.session.frame_info()
        if (
            status.get("capture_pending")
            or status.get("pending_update")
            or not frame
            or not frame["complete"]
            or not frame.get("current_scene")
            or frame["generation"] != status["generation"]
            or self.panel.canvas.frame_metadata.get("render_revision") != frame.get("render_revision")
        ):
            return False
        assert status["object_resources"] == 100000 and status["geometry_resources"] == 1, status
        assert status["render_device_ids"] == [self.f.device] and not status["source_diagnostics"], status
        assert not self.panel.canvas.image.isNull()
        return True

    def check_color(self, expected):
        frame = self.session.frame()
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])[
            :, :, :3
        ]
        assert np.isfinite(pixels).all()
        color = pixels.mean((0, 1))
        assert color.max() > 0.001, color
        expected = np.asarray(expected)
        np.testing.assert_allclose(color / color.max(), expected / expected.max(), atol=0.002, rtol=0)
        return color.tolist()

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            now = time.perf_counter()
            self.report["heartbeats"].append(dict(phase=self.stage, seconds=now - self.last))
            self.last = now
            assert now - self.started < 200, self.stage
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.auto.setChecked(False)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "initial"
                return
            if self.session is None:
                if self.panel.entry is None:
                    return
                self.session = self.panel.entry.session
            status = self.session.status()
            if self.stage == "initial":
                if not self.ready(status):
                    return
                self.pid = status["renderer_pid"]
                update = native_scene.capture(self.rop)
                update["rendering"]["max_bounce"] = -1
                self.panel.entry.apply(update, hou.time())
                self.stage = "source-rejected"
                return
            if self.stage == "source-rejected":
                if not status["update_rejected"] or status.get("capture_pending"):
                    return
                assert status["state"] == "failed" and not status["capture_restart_required"], status
                entry = registry.for_rop(self.rop, native_scene.configuration(self.rop))
                assert entry.session is self.session and status["renderer_pid"] == self.pid, status
                self.report["safe_rejection"] = status
                if not self.panel.render_button.isEnabled():
                    return
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "source-repaired"
                return
            if self.stage == "source-repaired":
                if not self.ready(status):
                    return
                assert status["renderer_pid"] == self.pid, status
                self.report["safe_repair"] = status
                self.hold_conversion()
                self.f.obj.parm("tx").set(0.01)
                self.submit()
                self.stage = "converted"
                return
            if self.stage == "converted":
                if not self.convert_entered.is_set():
                    return
                self.discard_entered, self.discard_release = self.barrier()

                def held_discard(job):
                    self.discard_entered.set()
                    assert self.discard_release.wait(8), "Host failed to release cancellation cleanup"
                    result = self.original_discard(job)
                    self.discards.append(result)
                    return result

                ch._native.discard_apply = held_discard
                began = time.perf_counter()
                self.panel.cancel()
                self.report["stop_seconds"] = time.perf_counter() - began
                assert self.report["stop_seconds"] < 0.1
                ch._native.convert_apply = self.original_convert
                self.convert_release.set()
                self.stage = "discard-held"
                self.held_at = now
                self.edits = 0
                return
            if self.stage == "discard-held":
                if not self.discard_entered.is_set():
                    return
                assert self.session._active_capture["stage"] == "discard"
                if self.edits < 4:
                    self.f.camera.parm("tx").set(0.01 * self.edits)
                    self.f.obj.parm("tx").set(0.02 + 0.01 * self.edits)
                    self.f.shader.parmTuple("color").set((0.2, 0.6, 0.1))
                    self.latest = self.submit()
                    self.edits += 1
                    assert self.session._pending_capture is not None
                    return
                if now - self.held_at < 0.5:
                    return
                beats = sum(row["phase"] == "discard-held" for row in self.report["heartbeats"])
                assert beats >= 8, beats
                self.report["discard_hold_heartbeats"] = beats
                ch._native.discard_apply = self.original_discard
                self.discard_release.set()
                self.stage = "restarted"
                return
            if self.stage == "restarted":
                if not self.ready(status):
                    return
                assert status["generation"] == self.latest and status["renderer_pid"] == self.pid, status
                self.report["restarted_color"] = self.check_color((0.2, 0.6, 0.1))
                assert (
                    self.discards
                    and not self.discards[0]["on_host_thread"]
                    and self.discards[0]["snapshot_objects"] == 100000
                ), self.discards
                self.report.update(restarted=status, discards=self.discards, diagnostics=self.session.diagnostics())
                # An internal cleanup failure must be a real failed session,
                # rather than a recoverable source warning or an endless poll.
                self.hold_conversion()
                self.f.obj.parm("tx").set(0.08)
                self.submit()
                self.stage = "failure-converted"
                return
            if self.stage == "failure-converted":
                if not self.convert_entered.is_set():
                    return

                def fail_discard(job):
                    raise RuntimeError("Deliberate owned cleanup failure")

                ch._native.discard_apply = fail_discard
                self.panel.cancel()
                ch._native.convert_apply = self.original_convert
                self.convert_release.set()
                self.stage = "failed"
                return
            if self.stage == "failed":
                if status["state"] != "failed":
                    return
                assert (
                    "Deliberate owned cleanup failure" in status["error"]
                    and not status["update_rejected"]
                    and status["capture_restart_required"]
                ), status
                assert not self.panel.canvas.image.isNull()
                try:
                    self.session.apply(dict(camera_path=self.f.camera.path()), hou.time())
                except RuntimeError as error:
                    assert "cleanup failed" in str(error), str(error)
                else:
                    raise AssertionError("A failed cleanup session accepted another capture")
                if not self.panel.render_button.isEnabled():
                    return
                self.report["internal_failure"] = status
                ch._native.discard_apply = self.original_discard
                self.f.shader.parmTuple("color").set((0.1, 0.2, 0.7))
                self.retire_entered, self.retire_release = self.barrier()

                def held_retire(handle):
                    self.retire_entered.set()
                    assert self.retire_release.wait(8), "Host failed to release capture retirement"
                    return self.original_retire(handle)

                ch._native.retire_capture = held_retire
                self.target_handle = self.session._handle
                began = time.perf_counter()
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.report["restart_submission_seconds"] = time.perf_counter() - began
                assert self.report["restart_submission_seconds"] < 0.1
                self.stage = "request-restart"
                self.held_at = now
                return
            if self.stage == "request-restart":
                self.replacement = self.panel.entry.session
                if self.replacement is self.session:
                    return
                self.stage = "retire-held"
                self.held_at = now
                return
            if self.stage == "retire-held":
                if not self.retire_entered.is_set():
                    return
                assert status["state"] == "closing", status
                self.f.camera.parm("tx").set(0.1)
                if now - self.held_at < 0.5:
                    return
                beats = sum(row["phase"] == "retire-held" for row in self.report["heartbeats"])
                assert beats >= 8, beats
                self.report["retire_hold_heartbeats"] = beats
                ch._native.retire_capture = self.original_retire
                self.retire_release.set()
                self.stage = "closed"
                return
            if self.stage == "closed":
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                life = status["scene_lifecycle"]
                assert life["retired_scenes"] == life["retired_snapshots"] == 0, life
                retirement = self.session.diagnostics()["capture"]["retirement"]
                assert not retirement["on_host_thread"] and retirement["snapshot_objects"] == 100000, retirement
                assert self.close_times and all(row["on_host_thread"] for row in self.close_times), self.close_times
                self.report.update(closed=status, retirement=retirement, host_finalization=self.close_times)
                self.session = self.replacement
                self.target_handle = self.session._handle
                self.stage = "fresh-after-failure"
                return
            if self.stage == "fresh-after-failure":
                if not self.ready(status):
                    return
                assert status["renderer_pid"] != self.pid, status
                self.report["fresh_color"] = self.check_color((0.1, 0.2, 0.7))
                image = self.panel.canvas.image
                blue = max(
                    image.pixelColor(x, y).blue() - image.pixelColor(x, y).green()
                    for y in range(0, image.height(), 7)
                    for x in range(0, image.width(), 7)
                )
                assert blue > 8, blue
                self.report["fresh_display_max_blue_minus_green"] = blue
                self.report["fresh_after_failure"] = status
                began = time.perf_counter()
                self.panel.dispose()
                registry.close_all()
                self.report["close_submission_seconds"] = time.perf_counter() - began
                assert self.report["close_submission_seconds"] < 0.1
                self.stage = "final-closed"
                return
            if self.stage == "final-closed":
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                self.report["fresh_closed"] = status
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False
            if not self.finished:
                self.save()

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        for barrier in self.barriers:
            barrier.set()
        ch._native.convert_apply = self.original_convert
        ch._native.discard_apply = self.original_discard
        ch._native.retire_capture = self.original_retire
        ch._native.close = self.original_close
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(
            result="failed" if error else "passed", error=error, seconds=time.perf_counter() - self.started
        )
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
