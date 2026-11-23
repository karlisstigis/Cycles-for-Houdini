# SPDX-License-Identifier: Apache-2.0
"""Terminate an actively tracing worker with the heavy viewport/two viewers open."""

import json
import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from cycles_houdini import registry, native_scene

os.environ["CYCLES_HOUDINI_BENCH_REPETITIONS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "1"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="heavy_failure_fixture"
)["HeavyResponse"]


class ActiveFailure(HeavyResponse):
    def load_scene(self, root, example):
        from worker_failure_fixture import build

        self.report["fixture_source"] = build(self.path.parent / "fixture")

    def configure_scene(self):
        # Remain actively tracing until the deliberate termination. Defaults
        # may converge before the native settings pane has finished opening.
        self.rop.setParms(
            dict(
                samples=self.samples,
                ipr_samples=self.samples,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                time_limit=0,
                use_denoise=0,
                ipr_use_denoise=0,
            )
        )

    def tick(self):
        if self.stage not in ("settings", "worker-failed", "worker-recovering", "failure-stop", "failure-cancelled"):
            return super().tick()
        try:
            entry = registry.get(self.key)
            if self.stage == "settings":
                if time.monotonic() < self.next_at:
                    return
                assert self.settings_pane.currentNode() == self.rop
                status = entry.session.status()
                assert status["state"] == "rendering" and not entry.session.frame_info()["complete"], status
                self.old_pid = status["renderer_pid"]
                self.old_native = list(self.viewer.pixel("C", 480, 270))
                self.report["active_failure_before"] = status
                self.report["phase"] = "terminate-active-worker"
                self.failure_at = time.monotonic()
                entry.session._controller.backend.process.kill()
                self.stage = "worker-failed"
            elif self.stage == "worker-failed":
                if entry.session.status()["state"] != "failed":
                    if time.monotonic() - self.failure_at > 10:
                        raise TimeoutError("Worker failure was not detected")
                    return
                assert entry.session.frame_info() is not None and self.panel.canvas.generation > 0
                self.report["active_failure"] = entry.session.status()
                self.report["failure_detection_seconds"] = time.monotonic() - self.failure_at
                self.before = entry.session.status()["generation"]
                self.report["phase"] = "recover-worker"
                self.recovery_at = time.monotonic()
                self.camera.parm("tx").set(self.camera_x + 0.3)
                entry.apply(native_scene.capture(self.rop), hou.time())
                self.stage = "worker-recovering"
            elif self.stage == "worker-recovering":
                if not self.ready(entry):
                    if time.monotonic() - self.recovery_at > 30:
                        raise TimeoutError("Heavy scene recovery stalled")
                    return
                pixel = list(self.viewer.pixel("C", 480, 270))
                if max(abs(a - b) for a, b in zip(pixel, self.old_native)) < 0.001:
                    return
                status = entry.session.status()
                assert status["renderer_pid"] != self.old_pid and status["backend_epoch"] == 2, status
                assert status["geometry_resources"] == self.report["fixture"]["geometry_resources"]
                assert status["object_resources"] == self.report["fixture"]["objects"]
                assert len(registry.entries()) == 1 and self.panel.entry is entry
                self.report["active_recovery"] = status
                self.report["recovered_native_pixel"] = pixel
                self.report["recovery_to_two_viewers_seconds"] = time.monotonic() - self.recovery_at
                self.report["phase"] = "active-stop"
                self.stage = "failure-stop"
            elif self.stage == "failure-stop":
                started = time.perf_counter()
                self.panel.cancel()
                self.report["active_cancel_submission_seconds"] = time.perf_counter() - started
                self.cancel_at = time.monotonic()
                self.stage = "failure-cancelled"
            elif self.stage == "failure-cancelled":
                if entry.session.status()["state"] != "cancelled":
                    if time.monotonic() - self.cancel_at > 10:
                        raise TimeoutError("Active render cancellation stalled")
                    return
                self.report["cancel_completion_seconds"] = time.monotonic() - self.cancel_at
                self.finish()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            try:
                registry.close_all()
            except Exception:
                pass
            hou.exit(1, suppress_save_prompt=True)


workflow = ActiveFailure()
