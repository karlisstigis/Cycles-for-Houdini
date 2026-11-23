# SPDX-License-Identifier: Apache-2.0
"""ON-SCREEN: drags in the Cycles Render panel (the owner's route), with counters.

Opens one floating Cycles Render pane tab (no Render View). Phases of 3 s, 30 edits
per second each: camera move, light move, light intensity, light size, object move.
Transform phases hold a synthetic mouse button (as a handle drag does) so the
gesture rules see a held mouse. Per phase it reports: updates sent by kind and the
narrow path / full-read reason, main-thread ms per step (panel poll, capture, apply,
pose preview), Houdini event-loop turns per second, Qt heartbeat gaps (viewport
hiccups), distinct images painted and edit-to-image latency.

Scene: built in memory (default) or the owner's private Threads scene opened read-only and
never saved (CYCLES_HOUDINI_PANEL_BENCH_SCENE=threads, frame 275; the file is named by
CYCLES_HOUDINI_PANEL_BENCH_THREADS_FILE, otherwise the run is skipped). Run only with the
owner's permission: scripts/run-gui-test.ps1 tests/houdini_panel_light_drag_gui_benchmark.py
"""

from collections import defaultdict
import json
import math
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import native_scene, object_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.panel import RenderPanel
from cycles_houdini.scene import camera_properties, light_properties, object_properties
from support.private_scenes import scene_file

THREADS = "CYCLES_HOUDINI_PANEL_BENCH_THREADS_FILE"
PHASES = ("camera", "light_move", "light_intensity", "light_size", "object_move")


def mark(stage):
    """Breadcrumb beside the report, flushed to disk: where a hard crash happened."""
    with open(Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).with_name("stages.log"), "a", encoding="utf-8") as stream:
        stream.write(f"{time.time():.3f} {stage}\n")
        stream.flush()
        os.fsync(stream.fileno())


HOLD = os.environ.get("CYCLES_HOUDINI_PANEL_BENCH_HOLD", "1") == "1"


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.scene = os.environ.get("CYCLES_HOUDINI_PANEL_BENCH_SCENE", "simple")
        mark("start " + self.scene)
        if self.scene == "threads":
            try:
                hou.hipFile.load(
                    str(scene_file(THREADS, "the owner's private Threads scene")),
                    suppress_save_prompt=True,
                    ignore_load_warnings=True,
                )
            except hou.LoadWarning:
                pass
            mark("scene loaded")
            hou.setFrame(275)
            mark("frame 275")
            self.rop = hou.node("/out/threads_colored_cam11")
            self.light = hou.node("/obj/light7")
            self.camera = hou.node(self.rop.evalParm("camera"))
            self.actor = next(o for o in object_scene.selected(self.rop, hou.time()) if o.name() != "Threads")
        else:
            obj = hou.node("/obj")
            self.camera = obj.createNode("cam", "bench_cam")
            self.camera.setParms(dict(ty=3, tz=9, rx=-15, resx=1280, resy=720))
            camera_properties.install(self.camera)
            self.light = obj.createNode("cycles::light::1", "bench_light")
            self.light.setParms(dict(tx=-3, ty=5, tz=4, rx=-50, ry=-30))
            ground = obj.createNode("geo", "bench_ground")
            object_properties.install(ground)
            grid = ground.createNode("grid")
            grid.parmTuple("size").set((20, 20))
            grid.setRenderFlag(True)
            self.actor = obj.createNode("geo", "bench_ball")
            object_properties.install(self.actor)
            sphere = self.actor.createNode("sphere")
            sphere.parm("type").set(menu_pick(sphere.parm("type"), "polygon"))
            sphere.setRenderFlag(True)
            self.actor.parm("ty").set(1)
            self.rop = hou.node("/out").createNode("cycles_ipr", "bench_rop")
            self.rop.setParms(dict(camera=self.camera.path(), resx=1280, resy=720))
        mark("scene nodes found")
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.base = {
            "camera": self.camera.evalParm("tx"),
            "light_move": self.light.evalParm("tx"),
            "light_intensity": self.light.evalParm("light_intensity"),
            "light_size": self.light.evalParm("areasizex") if self.light.parm("areasizex") else 1.0,
            "object_move": self.actor.evalParm("tx"),
        }
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 700))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        mark("pane created")
        self.holder = QtWidgets.QWidget()
        self.holder.setWindowTitle("bench mouse holder")
        self.holder.resize(60, 60)
        self.holder.show()
        self.panel = None
        self.report = dict(
            result="running", scene=self.scene, device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"], phases=[]
        )
        self.timings = defaultdict(list)
        self.kinds = defaultdict(int)
        self.reasons = defaultdict(int)
        self.edits = []  # (time, index)
        self.submissions = {}  # submission -> edit index
        self.paints = []  # (time, generation)
        self.loop_turns = 0
        self.restores = []
        workflow = self

        def wrap(owner, name, label, after=None):
            original = getattr(owner, name)

            def measured(*args, **kwargs):
                began = time.perf_counter()
                result = None
                try:
                    result = original(*args, **kwargs)
                    return result
                finally:
                    workflow.timings[label].append(time.perf_counter() - began)
                    if after:
                        after(args, kwargs, result)

            setattr(owner, name, measured)
            self.restores.append((owner, name, original))

        def after_apply(args, kwargs, result):
            if result is not None:
                workflow.last_submission = result
            if not workflow.moving or result is None:
                return
            entry, update = args[0], args[1]
            kind = (
                "pose"
                if kwargs.get("pose_preview")
                else "full"
                if "geometry" in update
                else "lights"
                if "lights" in update
                else "camera"
            )
            workflow.kinds[kind] += 1
            if kind in ("full", "lights"):
                workflow.reasons[getattr(entry, "narrow_reason", "") or kind] += 1
            workflow.submissions[result] = len(workflow.edits) - 1

        wrap(registry.Entry, "apply", "Entry.apply", after_apply)
        wrap(registry.Entry, "preview_object_poses", "Entry.preview_object_poses")
        wrap(native_scene, "capture", "native_scene.capture")
        wrap(RenderPanel, "poll", "RenderPanel.poll")
        wrap(RenderPanel, "render", "RenderPanel.render")
        self.stage = -1
        self.moving = False
        self.last_submission = None
        self.restored = False
        self.shown_since = None
        self.wait_started = time.monotonic()
        self.started = time.monotonic()
        self.timer = QtCore.QTimer()
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.edit)
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setTimerType(QtCore.Qt.PreciseTimer)
        self.heartbeat.setInterval(8)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()
        self.last_beat = time.perf_counter()
        self.gaps = []
        self.busy = False
        hou.ui.addEventLoopCallback(self.tick)

    def beat(self):
        now = time.perf_counter()
        if self.moving:
            self.gaps.append(now - self.last_beat)
            frame = getattr(self.panel.canvas, "frame_metadata", None) if self.panel else None
            generation = frame.get("generation") if frame else None
            sequence = frame.get("sequence") if frame else None
            if generation is not None and (not self.paints or self.paints[-1][2] != sequence):
                self.paints.append((now, generation, sequence))
        self.last_beat = now

    def edit(self):
        if not self.moving:
            return
        elapsed = time.monotonic() - self.phase_started
        if elapsed >= 3.0:
            self.stop_phase()
            return
        n = len(self.edits) + 1
        wave = math.sin(0.35 * n)
        phase = PHASES[self.stage]
        self.edits.append((time.perf_counter(), n))
        began = time.perf_counter()
        if phase == "camera":
            self.camera.parm("tx").set(self.base["camera"] + 0.15 * wave)
        elif phase == "light_move":
            self.light.parm("tx").set(self.base["light_move"] + 0.8 * wave)
        elif phase == "light_intensity":
            self.light.parm("light_intensity").set(self.base["light_intensity"] * (1 + 0.5 * wave))
        elif phase == "light_size":
            if self.light.parm("areasizex"):
                self.light.parm("areasizex").set(self.base["light_size"] * (1 + 0.3 * wave))
        else:
            self.actor.parm("tx").set(self.base["object_move"] + 0.5 * wave)
        self.timings["edit_parm"].append(time.perf_counter() - began)

    def start_phase(self):
        self.timings.clear()
        self.kinds.clear()
        self.reasons.clear()
        self.edits, self.submissions, self.paints, self.gaps = [], {}, [], []
        self.loop_turns = 0
        self.phase_started = time.monotonic()
        if HOLD and PHASES[self.stage] in ("camera", "light_move", "object_move", "light_intensity", "light_size"):
            QtTest.QTest.mousePress(self.holder, QtCore.Qt.LeftButton)
        self.moving = True
        self.timer.start()

    def stop_phase(self):
        self.moving = False
        self.timer.stop()
        if HOLD:
            QtTest.QTest.mouseRelease(self.holder, QtCore.Qt.LeftButton)
        latencies = []
        for at, generation, _ in self.paints:
            shown = max(
                (edit for submission, edit in self.submissions.items() if submission <= generation), default=None
            )
            if shown is not None and 0 <= shown < len(self.edits):
                latencies.append(at - self.edits[shown][0])
        stats = (
            lambda v: dict(
                count=len(v),
                median_ms=float(np.median(v)) * 1000,
                p95_ms=float(np.quantile(v, 0.95)) * 1000,
                max_ms=max(v) * 1000,
                total_ms=sum(v) * 1000,
            )
            if v
            else dict(count=0)
        )
        steps = max(1, len(self.edits))
        self.report["phases"].append(
            dict(
                phase=PHASES[self.stage],
                waited_before_seconds=self.waited,
                edits=len(self.edits),
                updates=dict(self.kinds),
                paths=dict(self.reasons),
                main_thread_ms_per_step={k: sum(v) * 1000 / steps for k, v in self.timings.items()},
                timings={k: stats(v) for k, v in self.timings.items()},
                event_loop_turns_per_second=self.loop_turns / 3.0,
                heartbeat=stats(self.gaps),
                images=len(self.paints),
                distinct_edits_shown=len(
                    {
                        e
                        for e in (
                            max((ed for s, ed in self.submissions.items() if s <= g), default=None)
                            for _, g, _ in self.paints
                        )
                        if e is not None
                    }
                ),
                latency=stats(latencies),
            )
        )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        self.settle_until = time.monotonic() + 2.0
        self.wait_started = time.monotonic()

    def tick(self):
        if self.moving:
            self.loop_turns += 1
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 300, "Panel drag benchmark timed out"
            if self.panel is None:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                mark("panel ready")
                self.panel.set_rop(self.rop)
                mark("rop set")
                self.panel.render_button.click()
                mark("render clicked")
                self.settle_until = time.monotonic() + 6.0
                return
            if self.moving or time.monotonic() < self.settle_until:
                return
            entry = self.panel.entry
            if entry is None:
                return
            assert not entry.error, entry.error
            if not self.restored:
                # Restore the previous phase's value before the next one.
                self.camera.parm("tx").set(self.base["camera"])
                self.light.parm("tx").set(self.base["light_move"])
                self.light.parm("light_intensity").set(self.base["light_intensity"])
                if self.light.parm("areasizex"):
                    self.light.parm("areasizex").set(self.base["light_size"])
                self.actor.parm("tx").set(self.base["object_move"])
                self.restored = True
                self.settle_until = time.monotonic() + 0.5
                return
            # A phase starts only when the image of the last update (first render, the
            # read after a release, the restore) has been on screen for 1.5 s. On a
            # heavy scene those take seconds; starting earlier measures them instead.
            frame = getattr(self.panel.canvas, "frame_metadata", None)
            generation = frame.get("generation") if frame else None
            if self.last_submission is None or generation is None or generation < self.last_submission:
                self.shown_since = None
                return
            if self.shown_since is None:
                self.shown_since = time.monotonic()
            if time.monotonic() - self.shown_since < 1.5:
                return
            self.waited = time.monotonic() - self.wait_started
            self.shown_since = None
            self.restored = False
            self.stage += 1
            if self.stage >= len(PHASES):
                self.finish()
                return
            self.settle_until = 0
            mark("phase " + PHASES[self.stage])
            self.start_phase()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

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
        self.holder.close()
        self.report.update(
            result="failed" if error else "passed", error=error, elapsed_seconds=time.monotonic() - self.started
        )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
