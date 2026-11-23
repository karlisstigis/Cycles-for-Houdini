# SPDX-License-Identifier: Apache-2.0
"""Object poses must paint during an ongoing drag, not only after it ends."""

import math
import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from cycles_houdini import _native, native_scene, registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ.setdefault("CYCLES_HOUDINI_BENCH_PACKED_COUNT", "100000")
os.environ["CYCLES_HOUDINI_BENCH_NO_MOTION"] = "1"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
module = runpy.run_path(
    str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="continuous_packed_fixture"
)
PackedResponse = module["PackedResponse"]
HeavyResponse = module["HeavyResponse"]


class ContinuousPacked(PackedResponse):
    def __init__(self):
        self.drag = None
        self.drags = []
        self.failures = []
        self.duration = float(os.environ.get("CYCLES_HOUDINI_DRAG_SECONDS", "5"))
        self.rate = float(os.environ.get("CYCLES_HOUDINI_DRAG_RATE", "20"))
        super().__init__()
        self.packed_paint = type(self.panel.canvas).paintEvent

        def painted(canvas, event):
            self.packed_paint(canvas, event)
            if canvas is not self.panel.canvas or self.drag is None:
                return
            metadata = getattr(canvas, "frame_metadata", {})
            entry = registry.get(self.key)
            if not entry or not metadata:
                return
            controller = entry.session._controller
            with controller.condition:
                revision = controller.physical
                updates = controller.cached["status"]["object_updates"]
                conversions = controller.cached["status"]["geometry_conversions"]
                generation = controller.committed
            if metadata.get("render_revision") != revision:
                return
            if self.stage == "drag" and updates > self.drag["last_painted_object_updates"]:
                self.drag["last_painted_object_updates"] = updates
                self.drag["object_paints"].append(
                    dict(
                        seconds=time.monotonic() - self.drag["started"],
                        physical_revision=revision,
                        logical_generation=generation,
                        object_updates=updates,
                    )
                )
            if self.stage == "drag" and conversions > self.drag["last_painted_conversions"]:
                self.drag["last_painted_conversions"] = conversions
                self.drag["geometry_paints"].append(
                    dict(
                        seconds=time.monotonic() - self.drag["started"],
                        physical_revision=revision,
                        geometry_conversions=conversions,
                    )
                )

        type(self.panel.canvas).paintEvent = painted
        self.report["scope"] = (
            "Real SOP viewport and one Cycles viewer; repeated native OBJ/camera parameter edits. Actual Qt paints with increased worker object updates verify object feedback before drag release. Timing samples are exploratory."
        )

    def start_drag(self, entry, camera, geometry=False):
        status = entry.session.status()
        self.drag = dict(
            started=time.monotonic(),
            camera=camera,
            geometry=geometry,
            inputs=[],
            object_paints=[],
            geometry_paints=[],
            duration=self.duration * (1.6 if geometry else 1),
            last_painted_object_updates=status["object_updates"],
            baseline_status=status,
            last_painted_conversions=status["geometry_conversions"],
            next_geometry=0.0,
            next_input=0.0,
            heartbeat_start=len(self.report["heartbeat_gaps"]),
        )
        self.next_input = time.monotonic()
        self.stage = "drag"
        self.report["phase"] = "geometry-objects-camera" if geometry else "objects-and-camera" if camera else "objects"

    def finish_checked(self, entry):
        self.report["continuous_drags"] = self.drags
        self.report["packed_fixture"] = dict(
            instances=self.instance_count,
            triangles_per_prototype=2 * (self.prototype_rows - 1) ** 2,
            construction="Native Grid, Transform and Copy to Points SOPs",
            motion_enabled=False,
        )
        self.report["liveness_failures"] = self.failures
        self.report["final_diagnostics"] = entry.session.diagnostics()
        self.panel.grab().save(str(self.path.with_name("continuous-packed-final.png")))
        type(self.panel.canvas).paintEvent = self.original_paint
        if self.failures:
            raise AssertionError("; ".join(self.failures))
        HeavyResponse.finish(self)

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 120:
                raise TimeoutError(self.stage)
            entry = registry.get(self.key)
            if not entry:
                return
            status = entry.session.status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"])
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                self.panel.auto.setChecked(False)
                self.pid = status["renderer_pid"]
                self.start_drag(entry, False)
            elif self.stage == "drag":
                now = time.monotonic()
                elapsed = now - self.drag["started"]
                if elapsed < self.drag["duration"]:
                    if now < self.next_input:
                        return
                    began = time.perf_counter()
                    x = 0.8 * math.sin(elapsed * 1.7) + 0.07 * elapsed
                    self.packed_obj.parm("tx").set(x)
                    if self.drag["geometry"] and elapsed >= self.drag["next_geometry"]:
                        self.deform.parm("rx").set(10 * math.sin(elapsed * 2.3))
                        self.drag["next_geometry"] = elapsed + 0.35
                    generation = entry.apply(native_scene.capture(self.rop), hou.time())
                    if self.drag["camera"]:
                        self.camera.parm("tx").set(self.camera_x + 0.2 * math.sin(elapsed * 0.8))
                        generation = entry.apply(native_scene.capture(self.rop, camera_only=True), hou.time())
                    self.drag["inputs"].append(
                        dict(seconds=elapsed, x=x, generation=generation, host_seconds=time.perf_counter() - began)
                    )
                    self.next_input = now + 1 / self.rate
                else:
                    self.drag["release_seconds"] = elapsed
                    self.drag["heartbeats_during_drag"] = (
                        len(self.report["heartbeat_gaps"]) - self.drag["heartbeat_start"]
                    )
                    self.latest = self.drag["inputs"][-1]["generation"]
                    self.before = self.latest - 1
                    self.stage = "settle"
                    self.report["phase"] = "settle"
            elif self.stage == "settle":
                if not self.ready(entry):
                    return
                status = entry.session.status()
                self.drag["settled_status"] = status
                self.drag["settled_seconds"] = time.monotonic() - self.drag["started"]
                self.drag["diagnostics"] = entry.session.diagnostics()
                assert status["renderer_pid"] == self.pid
                if not self.drag["geometry"]:
                    assert status["geometry_conversions"] == self.drag["baseline_status"]["geometry_conversions"]
                assert entry.session.diagnostics()["session_cancel_calls"] == 0
                controller = entry.session._controller
                assert (
                    controller.applied_roots_revision
                    == _native.navigation_info(controller.navigation[1])["root_revision"]
                )
                eligible = [p for p in self.drag["object_paints"] if p["seconds"] < self.drag["duration"] - 0.25]
                if len(eligible) < 2:
                    self.failures.append(
                        ("objects+camera" if self.drag["camera"] else "objects")
                        + ": fewer than two changed-object images painted before release"
                    )
                if (
                    self.drag["geometry"]
                    and len([p for p in self.drag["geometry_paints"] if p["seconds"] < self.drag["duration"] - 0.25])
                    < 2
                ):
                    self.failures.append(
                        "geometry+objects+camera: fewer than two changed-geometry images painted before release"
                    )
                if len(self.drag["inputs"]) < 20:
                    self.failures.append("Too few input events to exercise continuous updates")
                assert _native.navigation_context_equal(controller.acknowledged, controller.navigation[1])
                assert _native.navigation_snapshot(controller.acknowledged, controller.navigation[1]) is None
                self.drags.append(self.drag)
                self.drag = None
                if len(self.drags) == 1:
                    self.start_drag(entry, True)
                elif len(self.drags) == 2:
                    self.start_drag(entry, True, True)
                else:
                    self.finish_checked(entry)
            self.save()
        except Exception:
            self.report.update(
                result="failed",
                error=traceback.format_exc(),
                continuous_drags=self.drags,
                liveness_failures=self.failures,
            )
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            type(self.panel.canvas).paintEvent = self.original_paint
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = ContinuousPacked()
