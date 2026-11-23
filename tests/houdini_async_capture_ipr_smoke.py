# SPDX-License-Identifier: Apache-2.0
"""Artist IPR stays usable while a native conversion is deliberately held."""

import os
from pathlib import Path
import runpy
import threading
import time
import traceback
import faulthandler
import hou
from cycles_houdini import _native, native_scene, registry

faulthandler.enable(all_threads=True)

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="async_capture_fixture"
)["HeavyResponse"]


class AsyncCaptureWorkflow(HeavyResponse):
    def __init__(self):
        self.original_convert = _native.convert_apply
        self.original_prewarm = _native.prewarm_apply
        self.release = threading.Event()
        self.entered = threading.Event()
        self.events = []
        super().__init__()
        self.report["scope"] = (
            "One heavy SOP viewport and Cycles viewer. Deterministic conversion barriers verify edit coalescing, Stop/restart and nonblocking retirement; barriers are not performance benchmarks."
        )

    def configure_scene(self):
        self.transient = hou.node("/obj").createNode("geo", "async_transient")
        for child in self.transient.children():
            child.destroy()
        grid = self.transient.createNode("grid")
        grid.setParms(dict(rows=180, cols=180))
        self.transient.parm("tx").set(100)
        self.rop.parm("vobject").set("fx_courtyard async_transient")

    def restore_converter(self):
        _native.convert_apply = self.original_convert
        _native.prewarm_apply = self.original_prewarm

    def hold(self, scan=False):
        self.release = threading.Event()
        self.entered = threading.Event()
        release, entered = self.release, self.entered
        original = self.original_prewarm if scan else self.original_convert

        def held(job):
            assert threading.current_thread() is not threading.main_thread()
            entered.set()
            if not release.wait(8.0):
                raise TimeoutError("Conversion barrier was not released by the host event loop")
            return original(job)

        if scan:
            _native.prewarm_apply = held
        else:
            _native.convert_apply = held

    def submit(self, entry, payload):
        started = time.perf_counter()
        generation = entry.apply(payload, hou.time())
        self.events.append(dict(generation=generation, seconds=time.perf_counter() - started, phase=self.stage))
        return generation

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 100:
                raise TimeoutError(self.stage)
            entry = registry.get(self.key)
            if not entry:
                if self.stage == "retiring":
                    return self.finish_retirement()
                return
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                self.panel.auto.setChecked(False)
                self.initial_geometry = entry.session.status()["geometry_resources"]
                self.hold(scan=True)
                self.hero.parm("twist").set(2.95)
                self.submit(entry, native_scene.capture(self.rop))
                self.stage = "held-edits"
                self.report["phase"] = self.stage
                self.beats_before = len(self.report["heartbeat_gaps"])
                self.held_at = time.monotonic()
            elif self.stage == "held-edits":
                if not self.entered.is_set():
                    return
                if not hasattr(self, "pending_generation"):
                    # Geometry delta must survive later camera-only updates.
                    self.hero.parm("twist").set(3.4)
                    self.transient.destroy()
                    self.pending_generation = self.submit(entry, native_scene.capture(self.rop))
                    for offset in (0.12, 0.18, 0.24, 0.30, 0.36, 0.42):
                        self.camera.parm("tx").set(self.camera_x + offset)
                        self.pending_generation = self.submit(entry, native_scene.capture(self.rop, camera_only=True))
                    capture = entry.session.diagnostics()["capture"]
                    assert capture["active"] == 1 and capture["pending"] == 1, capture
                    assert max(item["seconds"] for item in self.events if item["phase"] == "held-edits") < 0.1, (
                        self.events
                    )
                if time.monotonic() - self.held_at < 0.7 or len(self.report["heartbeat_gaps"]) - self.beats_before < 10:
                    return
                self.report["held_scan_seconds"] = time.monotonic() - self.held_at
                self.report["held_scan_heartbeats"] = len(self.report["heartbeat_gaps"]) - self.beats_before
                self.release.set()
                self.restore_converter()
                self.before = self.pending_generation - 1
                self.stage = "latest-image"
                self.report["phase"] = self.stage
            elif self.stage == "latest-image":
                if not self.ready(entry):
                    return
                status = entry.session.status()
                assert status["generation"] == self.pending_generation
                assert status["geometry_resources"] == self.initial_geometry - 1
                self.report["coalescing"] = dict(status=status, diagnostics=entry.session.diagnostics())
                self.hold()
                self.hero.parm("twist").set(3.7)
                self.stopped_generation = self.submit(entry, native_scene.capture(self.rop))
                self.stage = "held-stop"
                self.report["phase"] = self.stage
            elif self.stage == "held-stop":
                if not self.entered.is_set():
                    return
                started = time.perf_counter()
                self.panel.cancel()
                self.report["stop_seconds"] = time.perf_counter() - started
                assert self.report["stop_seconds"] < 0.1
                self.hero.parm("twist").set(3.9)
                self.restarted = self.submit(entry, native_scene.capture(self.rop))
                self.release.set()
                self.restore_converter()
                self.before = self.restarted - 1
                self.stage = "restart-image"
                self.report["phase"] = self.stage
            elif self.stage == "restart-image":
                if not self.ready(entry):
                    return
                events = entry.session.diagnostics()["controller"]["events"]
                assert not any(
                    event.get("scene_submission", event["submission"]) == self.stopped_generation
                    and not event.get("navigation_preview", False)
                    for event in events
                ), events
                self.report["restart"] = dict(status=entry.session.status(), diagnostics=entry.session.diagnostics())
                self.hold()
                self.hero.parm("twist").set(4.1)
                self.submit(entry, native_scene.capture(self.rop))
                self.stage = "held-close"
                self.report["phase"] = self.stage
            elif self.stage == "held-close":
                if not self.entered.is_set():
                    return
                self.retired_session = entry.session
                started = time.perf_counter()
                registry.retire(self.key)
                self.report["close_submission_seconds"] = time.perf_counter() - started
                assert self.report["close_submission_seconds"] < 0.1
                self.stage = "retiring"
                self.report["phase"] = self.stage
                self.release.set()
                self.restore_converter()
            self.report["submissions"] = self.events
            self.save()
        except Exception:
            self.release.set()
            self.restore_converter()
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False

    def finish_retirement(self):
        status = self.retired_session.status()
        if status["state"] != "closed":
            if time.monotonic() - self.began > 100:
                self.report.update(result="failed", error="Retirement did not complete")
                self.save()
                hou.exit(1, suppress_save_prompt=True)
            return
        self.report["retired_status"] = status
        self.finish()


if __name__ == "__main__":
    workflow = AsyncCaptureWorkflow()
