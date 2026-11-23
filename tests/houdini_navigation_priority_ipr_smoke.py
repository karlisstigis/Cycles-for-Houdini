# SPDX-License-Identifier: Apache-2.0
"""Paint navigation before releasing held million-triangle packed captures."""

import os
from pathlib import Path
import runpy
import threading
import time
import traceback
import hou
from cycles_houdini import _native, native_scene, registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ.setdefault("CYCLES_HOUDINI_BENCH_PACKED_COUNT", "1000")
os.environ["CYCLES_HOUDINI_BENCH_NO_MOTION"] = "1"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
packed = runpy.run_path(str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="navigation_fixture")
PackedResponse = packed["PackedResponse"]
HeavyResponse = packed["HeavyResponse"]


class NavigationWorkflow(PackedResponse):
    def __init__(self):
        self.original_convert = _native.convert_apply
        self.original_prewarm = _native.prewarm_apply
        self.original_navigation = _native.navigation_snapshot
        self.release = threading.Event()
        self.entered = threading.Event()
        self.round = 0
        self.cases = []
        super().__init__()
        self.report["scope"] = (
            "One real SOP viewport/viewer; camera/object previews must paint while the heavy scan or conversion barrier is still held, then preserve geometry and latest navigation."
        )

    def hold(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        entered, release = self.entered, self.release
        original = self.original_prewarm if self.round == 0 else self.original_convert
        if self.round >= 2:

            def held_navigation(*args):
                camera_only = len(args) > 3 and args[3]
                if not camera_only:
                    assert threading.current_thread() is not threading.main_thread()
                    entered.set()
                    if not release.wait(12):
                        raise TimeoutError("Camera did not bypass held owned scene preparation")
                return self.original_navigation(*args)

            _native.navigation_snapshot = held_navigation
            return

        def held(job):
            entered.set()
            if not release.wait(12):
                raise TimeoutError("Navigation did not paint before releasing the capture barrier")
            return original(job)

        if self.round == 0:
            _native.prewarm_apply = held
        else:
            _native.convert_apply = held

    def restore(self):
        _native.prewarm_apply = self.original_prewarm
        _native.convert_apply = self.original_convert
        _native.navigation_snapshot = self.original_navigation

    def begin_round(self, entry):
        self.base_conversions = entry.session.status()["geometry_conversions"]
        if self.round == 2:
            self.packed_obj.parm("tx").set(0.65)
        self.hold()
        self.deform.parm("rx").set(13 + 18 * self.round)
        self.geometry_submission = entry.apply(native_scene.capture(self.rop), hou.time())
        self.stage = "held"
        self.report["phase"] = self.stage
        self.sent = False

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 100:
                raise TimeoutError(self.stage)
            entry = registry.get(self.key)
            if entry is None:
                if self.stage == "retiring-preparation":
                    retired = self.retired_session.status()
                    if retired["state"] == "closed":
                        assert retired["owned_geometry_bytes"] == 0
                        self.report["retired_preparation"] = retired
                        self.report["navigation_cases"] = self.cases
                        self.restore()
                        type(self.panel.canvas).paintEvent = self.original_paint
                        HeavyResponse.finish(self)
                return
            status = entry.session.status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"])
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                self.panel.auto.setChecked(False)
                self.pid = status["renderer_pid"]
                self.begin_round(entry)
            elif self.stage == "held":
                if not self.entered.is_set():
                    return
                if not self.sent:
                    self.navigation_started = time.monotonic()
                    if self.round == 2:
                        for x in (0.7, 0.72):
                            self.packed_obj.parm("tx").set(x)
                            entry.apply(native_scene.capture(self.rop), hou.time())
                    if self.round == 0:
                        # A second full edit still has to survive camera-only
                        # previews; it is not silently replaced by navigation.
                        self.packed_obj.parm("tx").set(0.35)
                        entry.apply(native_scene.capture(self.rop), hou.time())
                    for offset in (0.15, 0.22, 0.31):
                        self.camera.parm("tx").set(self.camera_x + offset + 0.1 * self.round)
                        self.latest = entry.apply(native_scene.capture(self.rop, camera_only=True), hou.time())
                    self.sent = True
                    return
                image = entry.session.frame_info()
                if not (
                    image
                    and image["generation"] == self.latest
                    and image.get("current_scene")
                    and self.panel.canvas.generation == self.latest
                    and self.panel.canvas.frame_metadata.get("render_revision") == status["render_revision"]
                    and self.painted.get(self.latest)
                ):
                    return
                assert not self.release.is_set()
                capture = entry.session.diagnostics()["capture"]
                assert status["pending_update"], status
                if self.round < 2:
                    assert capture["active"] == 1 and status["capture_pending"], (capture, status)
                else:
                    assert entry.session.diagnostics()["controller"]["preparing"] == 1
                assert status["renderer_pid"] == self.pid
                events = entry.session.diagnostics()["controller"]["events"]
                # Fair scheduling may paint the newest camera together with a
                # ready object preview. Verify actual accepted camera state and
                # physical paint, rather than requiring an internal path label.
                preview = next(
                    (
                        e
                        for e in reversed(events)
                        if e["submission"] == self.latest
                        and e.get("navigation_kind") in ("camera", "objects")
                        and e["accepted"]
                    ),
                    None,
                )
                assert preview is not None, events
                controller = entry.session._controller
                assert _native.navigation_context_equal(controller.acknowledged, controller.navigation[1])
                assert (
                    _native.navigation_snapshot(controller.acknowledged, controller.navigation[1], False, True) is None
                )
                assert status["geometry_conversions"] == self.base_conversions
                self.cases.append(
                    dict(
                        stage=("scan", "convert", "scene-preparation")[self.round],
                        preview_before_release=True,
                        preview_seconds=time.monotonic() - self.navigation_started,
                        requested_generation=self.latest,
                        painted_generation=self.panel.canvas.generation,
                        preview_navigation_kind=preview["navigation_kind"],
                        camera_matches_latest=True,
                        status=status,
                        capture=capture,
                        controller_events=events,
                    )
                )
                self.panel.grab().save(str(self.path.with_name("navigation-" + str(self.round) + "-preview.png")))
                self.release.set()
                self.restore()
                self.before = self.latest - 1
                self.stage = "refined"
                self.report["phase"] = self.stage
            elif self.stage == "refined":
                if not self.ready(entry):
                    return
                status = entry.session.status()
                assert status["renderer_pid"] == self.pid
                assert status["generation"] == self.latest and status["committed_generation"] == self.latest, status
                assert status["geometry_conversions"] > self.base_conversions, status
                assert not status["capture_pending"] and not status["pending_update"], status
                controller = entry.session._controller
                assert (
                    controller.applied_roots_revision
                    == _native.navigation_info(controller.navigation[1])["root_revision"]
                )
                assert entry.session.diagnostics()["session_cancel_calls"] == 0
                self.cases[-1]["combined_status"] = status
                self.cases[-1]["combined_diagnostics"] = entry.session.diagnostics()
                self.panel.grab().save(str(self.path.with_name("navigation-" + str(self.round) + "-refined.png")))
                if self.round < 2:
                    self.round += 1
                    self.begin_round(entry)
                else:
                    self.report["navigation_cases"] = self.cases
                    self.report["packed_fixture"] = dict(
                        instances=self.instance_count, triangles_per_prototype=2 * (self.prototype_rows - 1) ** 2
                    )
                    self.round = 3
                    self.hold()
                    self.packed_obj.parm("tx").set(0.8)
                    self.stopped_preparation = entry.apply(native_scene.capture(self.rop), hou.time())
                    self.stage = "stop-preparation"
            elif self.stage == "stop-preparation":
                if not self.entered.is_set():
                    return
                began = time.perf_counter()
                self.panel.cancel()
                self.report["stop_preparation_seconds"] = time.perf_counter() - began
                assert self.report["stop_preparation_seconds"] < 0.1
                self.packed_obj.parm("tx").set(0.9)
                self.latest = entry.apply(native_scene.capture(self.rop), hou.time())
                self.release.set()
                self.restore()
                self.before = self.latest - 1
                self.stage = "restart-preparation"
            elif self.stage == "restart-preparation":
                if not self.ready(entry):
                    return
                events = entry.session.diagnostics()["controller"]["events"]
                assert not any(e.get("scene_submission") == self.stopped_preparation for e in events)
                self.report["restart_preparation"] = entry.session.status()
                self.hold()
                self.packed_obj.parm("tx").set(1.1)
                entry.apply(native_scene.capture(self.rop), hou.time())
                self.stage = "close-preparation"
            elif self.stage == "close-preparation":
                if not self.entered.is_set():
                    return
                self.retired_session = entry.session
                began = time.perf_counter()
                registry.retire(self.key)
                self.report["close_preparation_seconds"] = time.perf_counter() - began
                assert self.report["close_preparation_seconds"] < 0.1
                self.release.set()
                self.restore()
                self.stage = "retiring-preparation"
        except Exception:
            self.release.set()
            self.restore()
            self.report.update(result="failed", error=traceback.format_exc(), navigation_cases=self.cases)
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = NavigationWorkflow()
