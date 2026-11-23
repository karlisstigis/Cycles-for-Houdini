# SPDX-License-Identifier: Apache-2.0
"""A live local final survives HIP replacement; explicit Play transfers and Stop wins."""

import json
import os
from pathlib import Path
import time
import traceback
import hou
from PySide6 import QtCore, QtTest
import pass_fixtures as parms
from gui_shutdown import retire_and_exit
from cycles_houdini import background_render, registry
from cycles_houdini.background_jobs import TERMINAL
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.started = time.monotonic()
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "HIP Transfer Jobs"))
        self.report = dict(result="running", checks=[])
        self.phase = "initial"
        self.busy = self.finished = False
        self.sessions = []
        self.jobs = []
        self.create_scene("before")
        self.timer = QtCore.QTimer()
        self.timer.setInterval(25)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def create_scene(self, label):
        geo = hou.node("/obj").createNode("geo", "hip_" + label, run_init_scripts=False)
        box = geo.createNode("box")
        box.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "ipr_" + label)
        self.disk = hou.node("/out").createNode("cycles_ipr", "disk_" + label)
        for rop in (self.rop, self.disk):
            parms.set_parameters(
                rop,
                dict(
                    vobject=geo.path(),
                    camera=camera.path(),
                    resx=96,
                    resy=64,
                    samples=1048576,
                    time_limit=15,
                    adaptive_sampling=0,
                    use_denoise=0,
                    ipr_samples=1048576,
                    ipr_adaptive_sampling=0,
                    ipr_use_denoise=0,
                    allowmotionblur=0,
                    render_threads=2,
                    use_material_assignments=0,
                    material_kind="emission",
                    emission_strength=1,
                    trange=0,
                ),
            )
            set_devices(rop, "CPU")
            rop.parm("picture").set(str(self.path.parent / (label + ".exr")))
        self.panel = RenderPanel()
        self.panel.set_rop(self.rop)
        self.panel.resize(1100, 650)
        self.panel.show()
        self.panel.queue_render()

    def record(self, message):
        self.report["checks"].append(message)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 90, self.phase
            p = self.panel
            if self.phase == "initial":
                if not p.entry or p.entry.session.status().get("backend_state") != "rendering":
                    return
                self.original = p.entry.session
                self.sessions.append(self.original)
                self.job = background_render.launch(self.disk)
                self.jobs.append(self.job)
                self.phase = "before_clear"
                return
            if self.phase == "before_clear":
                if (
                    self.job.status()["state"] != "rendering"
                    or p.entry.session.status().get("backend_state") != "paused"
                ):
                    return
                self.phase = "clearing"
                hou.ui.postEventCallback(self.replace_scene)
                return
            if self.phase == "after_clear":
                assert registry.local_final_active() and self.job.status()["state"] not in TERMINAL
                if not p._deferred_render:
                    return
                assert p.entry is None and not registry.entries()
                self.record("HIP replacement preserves active local final lease and defers new-scene Play")
                self.job.stop()
                self.phase = "new_ipr"
                return
            if self.phase == "new_ipr":
                if (
                    registry.local_final_active()
                    or not p.entry
                    or p.entry.session.status().get("backend_state") != "rendering"
                ):
                    return
                self.sessions.append(p.entry.session)
                assert p.entry.session is not self.original
                assert self.original.status()["state"] == "closed"
                self.record("new-scene Play starts automatically after the surviving local final releases")
                self.second = background_render.launch(self.disk)
                self.jobs.append(self.second)
                self.phase = "new_stop"
                return
            if self.phase == "new_stop":
                if not registry.local_final_active() or p.entry.session.status().get("backend_state") != "paused":
                    return
                QtTest.QTest.mouseClick(p.stop_button, QtCore.Qt.LeftButton)
                self.second.stop()
                self.phase = "stopped"
                return
            if self.phase == "stopped":
                if registry.local_final_active() or self.second.status()["state"] not in TERMINAL:
                    return
                if p.entry.session.status().get("backend_state") != "cancelled":
                    return
                assert p._stopped and not p._deferred_render
                self.record("manual Stop after HIP transfer prevents automatic restart")
                self.finish()
                return
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def replace_scene(self):
        if self.finished:
            return
        self.busy = True
        try:
            self.panel.dispose()
            self.panel.close()
            hou.hipFile.clear(suppress_save_prompt=True)
            assert registry.local_final_active() and not registry.entries()
            self.create_scene("after")
            self.phase = "after_clear"
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.report.update(
            error=error,
            phase=self.phase,
            elapsed_seconds=time.monotonic() - self.started,
            jobs=[job.status() for job in self.jobs],
        )
        for job in self.jobs:
            if job.status()["state"] not in TERMINAL:
                job.stop()
        self.panel.dispose()
        self.panel.close()
        retire_and_exit(self.path, self.report, self.sessions, 1 if error else 0, (registry.close_all,))


workflow = Workflow()
