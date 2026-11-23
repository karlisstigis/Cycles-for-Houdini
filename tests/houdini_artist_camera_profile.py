# SPDX-License-Identifier: Apache-2.0
"""Profile the reported artist scene in a private GUI process; never save it."""

import cProfile
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pstats
import threading
import time
import traceback

import hou
from PySide6 import QtCore
from cycles_houdini import device_selection, native_scene, registry
from cycles_houdini.panel import RenderPanel
from gui_shutdown import retire_and_exit
from support.private_scenes import artist_scene


class Profile:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.source = artist_scene("Cycles_Test/Cycles_Test.hiplc")
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.report = dict(result="running", stage="loading", source=str(self.source), phases=[])
        self.save()
        hou.hipFile.load(str(self.source), suppress_save_prompt=True, ignore_load_warnings=True)
        rops = [n for n in hou.node("/out").allSubChildren() if n.type().name() == "cycles_ipr"]
        self.report["rops"] = [n.path() for n in rops]
        self.rop = hou.node("/out/cycles_render") or rops[0]
        self.camera = hou.node(self.rop.evalParm("camera"))
        assert self.camera is not None
        self.extended = os.environ.get("CYCLES_HOUDINI_PROFILE_EXTENDED") == "1"
        self.targets = {}
        if self.extended:
            from cycles_houdini import object_scene

            objects = object_scene.selected(self.rop, hou.time())
            lights = [
                n
                for n in object_scene.selected(self.rop, hou.time(), lights=True)
                if not native_scene.world.is_environment(n)
            ]
            for kind, nodes in (("object", objects), ("light", lights)):
                node = next(n for n in nodes if n.parmTuple("t") and n.parmTuple("r"))
                self.targets[kind] = (node, node.parmTuple("t").eval(), node.parmTuple("r").eval())
                node.parmTuple("t").deleteAllKeyframes()
                node.parmTuple("r").deleteAllKeyframes()
            self.report["edit_targets"] = {kind: value[0].path() for kind, value in self.targets.items()}
        wanted = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        if wanted == "optix4090":
            import cycles_houdini as ch

            wanted = next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and "4090" in d["name"])
        device_selection.set_devices(self.rop, wanted)
        self.report.update(
            camera=self.camera.path(),
            rop=self.rop.path(),
            config=native_scene.configuration(self.rop),
            motion=native_scene.motion_configuration(self.rop, hou.time()),
            rendering=native_scene.rendering_configuration(self.rop, hou.time()),
        )
        self.base_t = self.camera.parmTuple("t").eval()
        self.base_r = self.camera.parmTuple("r").eval()
        self.optics = {
            name: self.camera.evalParm(name) for name in ("focal", "focus", "fstop") if self.camera.parm(name)
        }
        for name in self.optics:
            self.camera.parm(name).deleteAllKeyframes()
        self.camera.parmTuple("t").deleteAllKeyframes()
        self.camera.parmTuple("r").deleteAllKeyframes()
        self.panel = RenderPanel()
        self.panel.resize(1280, 800)
        self.panel.setWindowTitle("Cycles camera profiling — private scene copy")
        self.panel.show()
        self.panel.set_rop(self.rop)
        self.panel.queue_render()
        self.began = time.monotonic()
        self.phase = "initial"
        self.busy = False
        self.finished = False
        self.profiler = None
        self.gaps = []
        self.edits = []
        self.last_tick = time.perf_counter()
        self.move_at = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.report["stage"] = "initial"
        self.save()
        self.calls = {}
        self.patches = []
        from cycles_houdini import _native, queued

        self.host_thread = threading.get_ident()
        for owner, names in (
            (native_scene, ("configuration", "capture", "camera_signature")),
            (_native, ("prepare_apply", "finish_apply", "prepare_object_navigation")),
            (RenderPanel, ("render", "poll", "present_available", "sync_preview_controls", "request_update")),
        ):
            for name in names:
                original = getattr(owner, name)

                def measured(*args, _original=original, _name=owner.__name__ + "." + name, **kwargs):
                    if self.phase == "initial" or threading.get_ident() != self.host_thread:
                        return _original(*args, **kwargs)
                    started = time.perf_counter()
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        self.calls.setdefault(_name, []).append(time.perf_counter() - started)

                self.patches.append((owner, name, original))
                setattr(owner, name, measured)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str))

    def start_phase(self, name):
        self.phase = name
        self.phase_at = time.monotonic()
        self.gaps = []
        self.edits = []
        self.move_at = 0
        self.calls = {}
        self.before = self.panel.entry.session.status()
        self.profiler = cProfile.Profile()
        self.profiler.enable()
        self.report["stage"] = name
        self.save()

    def end_phase(self):
        self.profiler.disable()
        self.profiler.dump_stats(str(self.path.with_name(self.phase + ".pstats")))
        stream = io.StringIO()
        pstats.Stats(self.profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(75)
        self.path.with_name(self.phase + "-profile.txt").write_text(stream.getvalue())

        def summary(values):
            v = sorted(values)
            return dict(
                count=len(v),
                total=sum(v),
                p50=v[len(v) // 2] if v else 0,
                p95=v[min(len(v) - 1, int(len(v) * 0.95))] if v else 0,
                max=max(v, default=0),
            )

        after = self.panel.entry.session.status()
        self.report["phases"].append(
            dict(
                name=self.phase,
                heartbeat=summary(self.gaps),
                edit_seconds=summary(self.edits),
                calls={name: summary(v) for name, v in self.calls.items()},
                before=self.before,
                after=after,
                camera_only_pending=self.panel.entry.camera_only_pending,
                observer_camera_dependency=self.camera in self.panel.entry.observer.non_camera_dependencies,
            )
        )
        self.save()

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            at = time.perf_counter()
            gap = at - self.last_tick
            self.last_tick = at
            assert time.monotonic() - self.began < 220, self.phase
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if self.phase == "initial":
                if status.get("pending_update") or status.get("capture_pending") or not status.get("current_sample"):
                    return
                self.report["initial"] = status
                self.start_phase("rendering-camera")
                return
            self.gaps.append(gap)
            duration = time.monotonic() - self.phase_at
            if self.phase in ("rendering-camera", "stopped-camera") and at - self.move_at >= 0.04:
                self.move_at = at
                started = time.perf_counter()
                self.camera.parmTuple("t").set(
                    tuple(v + (0.15 * math.sin(duration * 2) if i == 0 else 0) for i, v in enumerate(self.base_t))
                )
                self.camera.parmTuple("r").set(
                    tuple(v + (0.5 * math.sin(duration * 1.7) if i == 1 else 0) for i, v in enumerate(self.base_r))
                )
                self.edits.append(time.perf_counter() - started)
            elif self.phase == "rendering-optics" and at - self.move_at >= 0.04:
                self.move_at = at
                started = time.perf_counter()
                for name, base in self.optics.items():
                    self.camera.parm(name).set(base * (1 + 0.01 * math.sin(duration * 2)))
                self.edits.append(time.perf_counter() - started)
            elif self.phase in ("rendering-object", "rendering-light") and at - self.move_at >= 0.04:
                self.move_at = at
                started = time.perf_counter()
                node, base_t, base_r = self.targets[self.phase.split("-")[1]]
                node.parmTuple("t").set(
                    tuple(v + (0.15 * math.sin(duration * 2) if i == 0 else 0) for i, v in enumerate(base_t))
                )
                node.parmTuple("r").set(
                    tuple(v + (3 * math.sin(duration * 1.7) if i == 1 else 0) for i, v in enumerate(base_r))
                )
                self.edits.append(time.perf_counter() - started)
            elif self.phase == "rendering-display" and at - self.move_at >= 0.04:
                self.move_at = at
                started = time.perf_counter()
                self.panel.exposure.setValue(0.3 * math.sin(duration * 2))
                self.edits.append(time.perf_counter() - started)
            if duration < (8 if self.phase.startswith("rendering-") else 3):
                return
            self.end_phase()
            if self.phase == "rendering-camera":
                self.start_phase("rendering-optics")
                return
            if self.phase == "rendering-optics":
                if self.extended:
                    self.start_phase("rendering-object")
                    return
                self.panel.cancel()
                self.start_phase("stopped-camera")
                return
            if self.phase == "rendering-object":
                self.start_phase("rendering-light")
                return
            if self.phase == "rendering-light":
                self.start_phase("rendering-display")
                return
            if self.phase == "rendering-display":
                self.start_phase("rendering-idle")
                return
            if self.phase == "rendering-idle":
                self.panel.cancel()
                self.start_phase("stopped-camera")
                return
            self.report["source_unchanged"] = hashlib.sha256(self.source.read_bytes()).hexdigest() == self.source_hash
            assert self.report["source_unchanged"]
            self.finish(0)
        except BaseException:
            if self.profiler:
                self.profiler.disable()
            self.report["error"] = traceback.format_exc()
            self.finish(1)
        finally:
            self.busy = False

    def finish(self, code):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        for owner, name, original in self.patches:
            setattr(owner, name, original)
        entry = self.panel.entry
        retire_and_exit(
            self.path,
            self.report,
            [entry.session] if entry else [],
            code,
            (self.panel.dispose, self.panel.close, registry.close_all),
        )


profile = Profile()
