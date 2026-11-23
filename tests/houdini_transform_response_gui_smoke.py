# SPDX-License-Identifier: Apache-2.0
"""Measure continuous OBJ edits in the artist scene without saving the scene."""

from support.paths import artifacts_root

from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore
from cycles_houdini import _native, device_selection, native_scene, object_scene, registry
from cycles_houdini.panel import RenderPanel


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        # The artist's live scene by default. Qualification pins a frozen copy:
        # viewport settings saved in the live scene change Houdini's own redraw
        # cost and so the measured input rate (as for the transform profile).
        self.source = Path(
            os.environ.get(
                "CYCLES_HOUDINI_PROFILE_SCENE",
                str(artifacts_root() / "examples/sop-fx-courtyard/Test.hiplc"),
            )
        )
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        hou.hipFile.load(self.source.as_posix(), suppress_save_prompt=True, ignore_load_warnings=True)
        self.rop = hou.node("/out/Test_2")
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.rop.setParms(
            dict(
                resx=320,
                resy=180,
                resolution_percent=100,
                ipr_resolution_percent=100,
                samples=8,
                ipr_samples=8,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
            )
        )
        self.actor = hou.node("/obj/circle1")
        output = self.actor.renderNode()
        self.deform = self.actor.createNode("xform", "transform_response_probe")
        self.deform.setInput(0, output)
        self.deform.setDisplayFlag(True)
        self.deform.setRenderFlag(True)
        self.light = next(
            node
            for node in object_scene.selected(self.rop, hou.time(), lights=True)
            if not native_scene.world.is_environment(node)
        )
        self.originals = {node.path(): node.parm("tx").eval() for node in (self.actor, self.light)}
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(900, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.report = dict(
            result="running",
            device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"],
            source=str(self.source),
            source_sha256=self.digest,
            phases=[],
        )
        self.timings = defaultdict(list)
        self.restores = []

        def wrap(owner, name, label):
            original = getattr(owner, name)

            def measured(*args, **kwargs):
                began = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    self.timings[label].append(time.perf_counter() - began)

            setattr(owner, name, measured)
            self.restores.append((owner, name, original))

        for owner, name in (
            (native_scene, "capture"),
            (native_scene.SceneObserver, "refresh"),
            (registry.Entry, "preview_object_poses"),
            (registry.Entry, "apply"),
            (RenderPanel, "poll"),
            (RenderPanel, "request_update"),
            (_native, "acquire_apply"),
            (_native, "prepare_apply"),
            (_native, "prepare_object_navigation"),
            (_native, "finish_apply"),
        ):
            wrap(owner, name, owner.__name__ + "." + name)
        self.stage = -1
        self.started = time.monotonic()
        self.busy = False
        self.moving = False
        self.edit_count = 0
        self.timer = QtCore.QTimer()
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.edit)
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setTimerType(QtCore.Qt.PreciseTimer)
        self.heartbeat.setInterval(8)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()
        self.last_beat = time.perf_counter()
        self.gaps = []
        self.baseline = None
        hou.ui.addEventLoopCallback(self.tick)

    def beat(self):
        now = time.perf_counter()
        if self.moving:
            self.gaps.append(now - self.last_beat)
        self.last_beat = now

    def edit(self):
        if not self.moving:
            return
        elapsed = time.monotonic() - self.phase_started
        if self.stage == 2:
            if elapsed > 0.6 and not self.broken:
                self.deform.parm("tx").set(0.2)
                self.broken = True
            elif elapsed > 1.6 and not self.repaired:
                self.deform.parm("tx").set(0)
                self.repaired = True
        if elapsed >= 3:
            self.gesture_timings = self.summarize()
            self.moving = False
            self.timer.stop()
            self.node.parm("tx").set(self.originals[self.node.path()] + 0.65)
            self.settling = time.monotonic()
            return
        self.edit_count += 1
        began = time.perf_counter()
        self.node.parm("tx").set(self.originals[self.node.path()] + 0.65 * math.sin(elapsed * 3))
        self.timings["edit_parm"].append(time.perf_counter() - began)

    def summarize(self):
        return {
            name: dict(
                count=len(values),
                total_ms=sum(values) * 1000,
                p95_ms=float(np.quantile(values, 0.95)) * 1000,
                max_ms=max(values) * 1000,
            )
            for name, values in self.timings.items()
            if values
        }

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 90, "Transform response test timed out"
            if self.panel is None:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.render_button.click()
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            frame = entry.session.frame_info()
            ready = (
                frame
                and frame["complete"]
                and status["state"] == "complete"
                and not status.get("pending_update")
                and not status.get("capture_pending")
                and frame["generation"] == status["generation"]
                and self.panel._live_generation == frame["generation"]
            )
            if self.stage == -1:
                if not ready:
                    return
                self.worker = status["renderer_pid"]
                self.stage = 0
                self.start_phase()
            elif self.moving:
                shown = self.panel.canvas.frame_metadata
                if shown and shown.get("render_revision", 0) != self.initial_revision:
                    self.preview_revisions.add(shown["render_revision"])
                self.report["current"] = dict(stage=self.stage, edits=self.edit_count, status=status)
            elif ready and time.monotonic() - self.settling > 0.6:
                pixels = np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
                assert np.isfinite(pixels).all()
                assert status["renderer_pid"] == self.worker
                if self.stage < 2:
                    assert status["geometry_conversions"] == self.before["geometry_conversions"], (
                        "OBJ movement reconverted geometry"
                    )
                else:
                    assert self.repaired and status["geometry_conversions"] > self.before["geometry_conversions"]
                    assert self.gesture_timings.get("_cycles_houdini.acquire_apply", {}).get("count", 0) >= 1, (
                        "A source edit was deferred behind transforms"
                    )
                    error = float(np.abs(np.clip(pixels, 0, 1) - np.clip(self.mesh_reference, 0, 1)).mean())
                    assert error < 0.001, (
                        "Repaired geometry/transform did not match the earlier accepted frame",
                        error,
                    )
                if self.stage == 1:
                    self.mesh_reference = pixels
                if os.environ.get("CYCLES_HOUDINI_TRANSFORM_ASSERT_FAST") == "1":
                    if self.stage < 2:
                        assert not self.gesture_timings.get("SceneObserver.refresh"), (
                            "Subscriptions rebuilt during the gesture"
                        )
                        assert not self.gesture_timings.get("_cycles_houdini.acquire_apply"), (
                            "Full source capture ran during the gesture"
                        )
                    assert len(self.preview_revisions) >= 2, "Transforms did not paint while movement continued"
                    assert self.edit_count >= 100, "Transform input was starved"
                self.report["phases"].append(
                    dict(
                        node=self.node.path(),
                        edits=self.edit_count,
                        gesture_timings=self.gesture_timings,
                        preview_revisions=sorted(self.preview_revisions),
                        timings=self.summarize(),
                        heartbeat_p95_ms=float(np.quantile(self.gaps, 0.95)) * 1000,
                        heartbeat_max_ms=max(self.gaps) * 1000,
                        heartbeat_count=len(self.gaps),
                        capture=entry.session.diagnostics()["capture"],
                        status=status,
                    )
                )
                self.node.parm("tx").set(self.originals[self.node.path()])
                self.stage += 1
                if self.stage == 3:
                    self.finish()
                    return
                self.start_phase()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def start_phase(self):
        self.node = (self.light, self.actor, self.actor)[self.stage]
        self.timings.clear()
        self.gaps.clear()
        self.edit_count = 0
        self.broken = self.repaired = False
        self.before = self.panel.entry.session.status()
        self.initial_revision = self.before["render_revision"]
        self.preview_revisions = set()
        self.phase_started = time.monotonic()
        self.moving = True
        self.timer.start()

    def finish(self, error=None):
        self.timer.stop()
        self.heartbeat.stop()
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        for owner, name, original in reversed(self.restores):
            setattr(owner, name, original)
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.digest
        self.report.update(
            result="failed" if error else "passed", error=error, elapsed_seconds=time.monotonic() - self.started
        )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
