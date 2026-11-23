# SPDX-License-Identifier: Apache-2.0
"""Separate native SOP cooking from our owned capture, without an engine.

Reuse the exact heavy animation fixture. Alternate three five-frame viewport
sequences with three capture-only sequences in one isolated GUI instance.
Capture-only handles do not construct a Cycles scene/session or upload a GPU
buffer. Heartbeats measure event delivery, not GPU presentation latency.
"""

import os
from pathlib import Path
import runpy
import time
import traceback
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
fixture = runpy.run_path(
    str(root / "tests/houdini_heavy_animation_benchmark.py"), run_name="viewport_animation_fixture"
)
HeavyResponse = fixture["HeavyResponse"]
HeavyAnimation = fixture["HeavyAnimation"]


class HeavyViewportBaseline(HeavyResponse):
    create_render_panel = False
    configure_scene = HeavyAnimation.configure_scene

    def __init__(self):
        super().__init__()
        assert self.panel is None and not registry.entries()
        self.diagnostic_title = "Cycles diagnostic: viewport only - rendering disabled"
        self.diagnostic_window = hou.qt.floatingPanelWindow(self.scene.floatingPanel())
        self.diagnostic_window.setWindowTitle(self.diagnostic_title)
        assert self.diagnostic_window.windowTitle() == self.diagnostic_title
        self.report.update(
            execution="no-renderer",
            render_viewers=[],
            sequences=[],
            diagnostic_window_title=self.diagnostic_title,
            scope="Same heavy five-frame fixture; native viewport and optionally owned HDK capture. No rendering session or GPU upload.",
            timing_policy="Frames 2-5 are warm measurements. Explicit HOM geometry() resolves the displayed SOP before capture. Qt gaps include redraw/event scheduling and are not pixel-presentation timestamps.",
        )
        self.order = [(0, "viewport"), (0, "capture"), (1, "capture"), (1, "viewport"), (2, "viewport"), (2, "capture")]
        self.order_index = 0
        self.capture_handle = None
        self.current_sequence = None
        self.next_at = time.monotonic() + 1.0
        self.stage = "prepare"
        self.report["phase"] = "viewport-settle"
        self.save()

    def prepare(self):
        if self.capture_handle is not None:
            ch._native.close(self.capture_handle)
            self.capture_handle = None
        repetition, condition = self.order[self.order_index]
        self.report["phase"] = f"setup-{condition}-{repetition}"
        began = time.monotonic()
        hou.setFrame(1)
        self.obj.renderNode().geometry()
        if condition == "capture":
            self.capture_handle = ch._native.create(
                self.device, 960, 540, self.samples, True, False, True, False, None, True
            )
            # Warm the resource ledger outside the measurement window, matching
            # later-frame conversion in an already established render session.
            owned = ch._native.apply(self.capture_handle, native_scene.capture(self.rop), hou.time())
            assert ch._native.snapshot_info(owned) == {
                k: v for k, v in self.report["fixture"].items() if k not in ("source_points", "source_primitives")
            }
            del owned
            self.previous_capture_extractions = ch._native.status(self.capture_handle)["extractions"]
        self.current_sequence = dict(
            condition=condition, repetition=repetition, setup_seconds=time.monotonic() - began, frames=[]
        )
        self.sequence_frame = 1
        self.next_at = time.monotonic() + 0.4
        self.stage = "frame"
        self.report["phase"] = f"settle-{condition}-{repetition}"

    def perform_frame(self):
        # Keep the diagnostic visibly distinct from the Cycles Render panel.
        # Title work is outside the timed host action.
        self.diagnostic_window.setWindowTitle(self.diagnostic_title)
        condition = self.current_sequence["condition"]
        repetition = self.current_sequence["repetition"]
        phase = f"{condition}-{repetition}-frame-{self.sequence_frame}"
        self.report["phase"] = phase
        began = time.monotonic()
        hou.setFrame(self.sequence_frame)
        frame_action_seconds = time.monotonic() - began
        cook_started = time.monotonic()
        detail = self.obj.renderNode().geometry()
        cook_seconds = time.monotonic() - cook_started
        assert detail.intrinsicValue("pointcount") == self.report["fixture"]["source_points"]
        assert detail.intrinsicValue("primitivecount") == self.report["fixture"]["source_primitives"]
        record = dict(
            frame=self.sequence_frame,
            phase=phase,
            twist=self.hero.parm("twist").eval(),
            parameter_action_seconds=frame_action_seconds,
            displayed_sop_resolve_seconds=cook_seconds,
            total_host_action_seconds=time.monotonic() - began,
            displacement_map=self.height_texture.parm("filename").eval(),
        )
        if self.capture_handle is not None:
            request_started = time.monotonic()
            request = native_scene.capture(self.rop)
            request_seconds = time.monotonic() - request_started
            capture_started = time.monotonic()
            owned = ch._native.apply(self.capture_handle, request, hou.time())
            record.update(
                request_seconds=request_seconds,
                capture_seconds=time.monotonic() - capture_started,
                capture_stats=ch._native.status(self.capture_handle),
                native_stats=ch._native.native_stats(self.capture_handle),
            )
            record["extraction_delta"] = record["capture_stats"]["extractions"] - self.previous_capture_extractions
            assert record["extraction_delta"] == (0 if self.sequence_frame == 1 else 1), record
            self.previous_capture_extractions = record["capture_stats"]["extractions"]
            del owned
            record["total_host_action_seconds"] = time.monotonic() - began
        assert not registry.entries() and not self.report["applies"]
        self.current_sequence["frames"].append(record)
        # Return to the real host loop and allow its viewport redraw to occur.
        self.next_at = time.monotonic() + 0.3
        self.stage = "observe"
        self.save()

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 150:
                raise TimeoutError("Viewport/capture comparison exceeded its limit")
            if time.monotonic() < self.next_at:
                return
            if self.stage == "prepare":
                self.prepare()
            elif self.stage == "frame":
                self.perform_frame()
            elif self.stage == "observe":
                if self.sequence_frame < 5:
                    self.sequence_frame += 1
                    self.stage = "frame"
                else:
                    self.report["sequences"].append(self.current_sequence)
                    self.order_index += 1
                    if self.order_index == len(self.order):
                        ch._native.close(self.capture_handle)
                        self.capture_handle = None
                        assert not registry.entries()
                        assert self.scene.curViewport().camera() == self.camera
                        assert self.diagnostic_window.windowTitle() == self.diagnostic_title
                        self.diagnostic_window.grab().save(str(self.path.with_suffix(".viewport.png")))
                        self.finish()
                        return
                    self.stage = "prepare"
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            if self.capture_handle is not None:
                try:
                    ch._native.close(self.capture_handle)
                except Exception:
                    pass
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


if __name__ == "__main__":
    workflow = HeavyViewportBaseline()
