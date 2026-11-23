# SPDX-License-Identifier: Apache-2.0
"""Real Qt controls and real local jobs: shared IPR leases, Stop and final retention."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
import pass_fixtures as parms
from gui_shutdown import retire_and_exit
from cycles_houdini import background_render, final_render, registry, rop_controls
from cycles_houdini.background_jobs import TERMINAL
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "Local Final Jobs"))
        self.started = self.last_beat = time.monotonic()
        self.phase = "initial_ipr"
        self.busy = self.finished = False
        self.jobs, self.sessions, self.beats = [], [], []
        self.report = dict(result="running", checks=[], events=[], device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        geo = hou.node("/obj").createNode("geo", "lease_subject", run_init_scripts=False)
        box = geo.createNode("box")
        box.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "lease_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "lease_shared_ipr")
        self.disk = hou.node("/out").createNode("cycles_ipr", "lease_disk_final")
        for rop in (self.rop, self.disk):
            parms.set_parameters(
                rop,
                dict(
                    vobject=geo.path(),
                    camera=camera.path(),
                    resx=128,
                    resy=96,
                    samples=1048576,
                    time_limit=3,
                    adaptive_sampling=0,
                    use_denoise=0,
                    ipr_samples=1048576,
                    ipr_adaptive_sampling=0,
                    ipr_use_denoise=0,
                    ipr_resolution_percent=100,
                    allowmotionblur=0,
                    render_threads=2,
                    use_material_assignments=0,
                    material_kind="emission",
                    emission_strength=1,
                    trange=0,
                    persistent_data=0,
                ),
            )
            rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
        set_devices(self.rop, [self.report["device"]])
        set_devices(self.disk, "CPU")
        self.rop.parm("picture").set(str(self.path.parent / "Cancelled Clapper.exr"))
        self.panels = [RenderPanel(), RenderPanel()]
        for index, panel in enumerate(self.panels):
            panel.set_rop(self.rop)
            panel.resize(1100, 620)
            panel.move(30 + index * 80, 30 + index * 60)
            panel.show()
            panel.queue_render()
        self.timer = QtCore.QTimer()
        self.timer.setInterval(25)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setInterval(10)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()

    def beat(self):
        now = time.monotonic()
        self.beats.append(now - self.last_beat)
        self.last_beat = now

    def click(self, button):
        assert button.isEnabled(), (self.phase, button.toolTip())
        QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)

    def state(self):
        return self.entry.session.status()

    def backend(self):
        status = self.state()
        return status.get("backend_state", status["state"])

    def event(self, name):
        self.report["checks"].append(name)
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                state=self.state(),
                final_active=registry.local_final_active(),
                final_paused=self.entry.final_paused,
                user_paused=self.entry.paused,
                stop_serial=self.entry.stop_serial,
            )
        )
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def disk_job(self, label, seconds=1, blocked=False):
        picture = self.path.parent / (label + ".exr")
        if blocked:
            picture.mkdir()
        self.disk.setParms(dict(time_limit=seconds, picture=str(picture)))
        # Exercise the artist-facing ROP QAction, including its snapshot/launch path.
        menu = rop_controls.show_controls(self.disk)
        action = next(item for item in menu.actions() if item.data() == "render_background")
        action.trigger()
        menu.close()
        job = background_render.last_job(self.disk)
        assert job is not None
        self.jobs.append(job)
        return job

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 180, self.phase
            left, right = self.panels
            view = left._final_view
            if view and view.job is not None and view.job not in self.jobs:
                self.jobs.append(view.job)
            if view and view.entry is not None and view.entry.session not in self.sessions:
                self.sessions.append(view.entry.session)
            if self.phase == "initial_ipr":
                if not left.entry or left.entry is not right.entry:
                    return
                self.entry = left.entry
                if self.backend() != "rendering" or any(panel.canvas.image.isNull() for panel in self.panels):
                    return
                self.sessions.append(self.entry.session)
                self.original_session = self.entry.session
                self.renderer_pid = self.state()["renderer_pid"]
                assert self.entry.views == 2
                if not getattr(self, "rollback_tested", False):
                    from cycles_houdini import viewer_final

                    real_view = viewer_final.FinalView

                    def fail_constructor(*args, **kwargs):
                        raise ValueError("QA constructor failure")

                    viewer_final.FinalView = fail_constructor
                    try:
                        left._final_queued = True
                        left._run_final_render()
                    finally:
                        viewer_final.FinalView = real_view
                    assert not registry.local_final_active() and left.entry is self.entry and self.entry.views == 2
                    self.event("real Qt panel constructor failure rolls back without acquiring a lease")
                    real_detach = left._detach

                    def fail_detach():
                        raise ValueError("QA detach failure")

                    left._detach = fail_detach
                    try:
                        left._final_queued = True
                        left._run_final_render()
                    finally:
                        left._detach = real_detach
                    assert not registry.local_final_active() and left.entry is self.entry and self.entry.views == 2
                    self.event("real Qt panel detach failure releases lease and balances IPR references")
                    real_attach = left._attach

                    def fail_attach(entry):
                        if entry.key == "viewer-final":
                            raise ValueError("QA final registration failure")
                        return real_attach(entry)

                    left._attach = fail_attach
                    try:
                        left._final_queued = True
                        left._run_final_render()
                    finally:
                        left._attach = real_attach
                    self.failed_view = left._final_view
                    assert self.failed_view.error and self.failed_view.job is not None
                    assert not self.failed_view.owned and self.failed_view.entry.session.status()["state"] == "closed"
                    self.jobs.append(self.failed_view.job)
                    self.phase = "rollback_wait"
                    return
                self.event("two panels share one running renderer")
                self.phase = "clapper_running"
                self.click(left.final_render_button)
                return
            if self.phase == "rollback_wait":
                if (
                    self.failed_view.job.status()["state"] not in TERMINAL
                    or registry.local_final_active()
                    or self.backend() != "rendering"
                ):
                    return
                assert self.failed_view.job.status()["state"] == "cancelled"
                self.event(
                    "real child registration failure closes remote display and releases only after child retirement"
                )
                self.rollback_tested = True
                left.queue_render()
                self.phase = "initial_ipr"
                return
            if self.phase == "clapper_running":
                if not view or not view.entry or view.entry.session.status()["state"] != "rendering":
                    return
                if self.backend() != "paused" or left.canvas.image.isNull():
                    return
                assert right.entry is self.entry and self.entry.session is self.original_session
                assert self.entry.final_paused and not self.entry.paused
                self.event("clapper pauses shared IPR without replacing it")
                self.stop_serial = self.entry.stop_serial
                self.phase = "manual_ipr_stop"
                self.click(right.stop_button)
                return
            if self.phase == "manual_ipr_stop":
                if self.backend() != "cancelled":
                    return
                assert self.entry.stop_serial > self.stop_serial
                self.phase = "clapper_cancelled"
                self.stop_at = time.monotonic()
                self.click(left.stop_button)
                return
            if self.phase == "clapper_cancelled":
                if not view or view.running or registry.local_final_active():
                    return
                assert view.job.status()["state"] == "cancelled", view.job.status()
                assert self.backend() == "cancelled", self.state()
                assert not (self.path.parent / "Cancelled Clapper.exr").exists()
                self.report["clapper_stop_seconds"] = time.monotonic() - self.stop_at
                self.event("manual Stop survives clapper cancellation and lease release")
                self.phase = "ipr_restarted"
                self.click(right.render_button)
                return
            if self.phase == "ipr_restarted":
                if self.backend() != "rendering" or not right._render_mode:
                    return
                assert self.entry.session is self.original_session and self.state()["renderer_pid"] == self.renderer_pid
                self.rop.setParms(dict(time_limit=0.7, picture=str(self.path.parent / "Completed Clapper.exr")))
                self.phase = "clapper_complete"
                self.saw_pause = False
                self.click(left.final_render_button)
                return
            if self.phase == "clapper_complete":
                self.saw_pause |= self.backend() == "paused" and self.entry.final_paused
                if not view or view.running or registry.local_final_active() or self.backend() != "rendering":
                    return
                if left.canvas.image.isNull() or left.canvas.full_size != (128, 96):
                    return
                assert self.saw_pause and view.job.status()["state"] == "completed", view.job.status()
                assert view.owned and left.entry is view.entry and right.entry is self.entry
                assert (self.path.parent / "Completed Clapper.exr").is_file()
                self.held = left.canvas.image.copy()
                self.event("clapper completion resumes original IPR and retains final image")
                self.phase = "manual_pause"
                self.click(right.pause_button)
                return
            if self.phase == "manual_pause":
                if self.backend() != "paused" or not self.entry.paused:
                    return
                self.disk_single = self.disk_job("Disk While User Paused", 0.7)
                self.phase = "disk_user_pause"
                return
            if self.phase == "disk_user_pause":
                if self.disk_single.status()["state"] not in TERMINAL or registry.local_final_active():
                    return
                assert self.disk_single.status()["state"] == "completed", self.disk_single.status()
                assert self.backend() == "paused" and self.entry.paused
                assert left.canvas.image == self.held
                self.event("Render to Disk preserves an existing user Pause and retained final image")
                self.phase = "resume_for_jobs"
                self.click(right.pause_button)
                return
            if self.phase == "resume_for_jobs":
                if self.backend() != "rendering":
                    return
                self.job_a = self.disk_job("Overlapping Disk A", 8)
                self.phase = "overlap_start"
                return
            if self.phase == "overlap_start":
                if self.backend() != "paused":
                    return
                self.job_b = self.disk_job("Overlapping Disk B", 8)
                self.phase = "overlap_cancel_a"
                return
            if self.phase == "overlap_cancel_a":
                if self.job_a.status()["state"] != "rendering" or self.job_b.status()["state"] != "rendering":
                    return
                assert self.backend() == "paused" and self.entry.final_paused
                self.job_a.stop()
                self.phase = "overlap_a_done"
                return
            if self.phase == "overlap_a_done":
                if self.job_a.status()["state"] not in TERMINAL:
                    return
                assert self.job_a.status()["state"] == "cancelled"
                assert self.job_b.status()["state"] not in TERMINAL
                assert registry.local_final_active() and self.backend() == "paused"
                self.event("first overlapping job release keeps IPR paused for the second job")
                self.job_b.stop()
                self.phase = "overlap_b_done"
                return
            if self.phase == "overlap_b_done":
                if (
                    self.job_b.status()["state"] not in TERMINAL
                    or registry.local_final_active()
                    or self.backend() != "rendering"
                ):
                    return
                assert self.job_b.status()["state"] == "cancelled"
                assert left.canvas.image == self.held
                self.event("last overlapping job release resumes the originally running IPR")
                self.disk_failed = self.disk_job("Blocked Output", 0.5, blocked=True)
                self.phase = "disk_error"
                return
            if self.phase == "disk_error":
                if (
                    self.disk_failed.status()["state"] not in TERMINAL
                    or registry.local_final_active()
                    or self.backend() != "rendering"
                ):
                    return
                assert self.disk_failed.status()["state"] == "failed", self.disk_failed.status()
                self.event("local output failure releases IPR suspension")
                self.phase = "foreground_queued"
                hou.ui.postEventCallback(self.foreground_disk)
                return
            if self.phase == "foreground_returned":
                if self.backend() != "rendering":
                    return
                assert not registry.local_final_active()
                assert self.report["foreground"]["saw_logical_pause"] and self.report["foreground"]["saw_backend_pause"]
                assert (self.path.parent / "Foreground Disk.exr").is_file()
                assert left.canvas.image == self.held
                self.event("foreground Render to Disk pauses and releases IPR through real ROP lifecycle")
                for index, panel in enumerate(self.panels):
                    assert panel.grab().save(str(self.path.parent / f"viewer-{index}.png"))
                self.finish()
                return
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def foreground_disk(self):
        if self.finished:
            return
        self.busy = True
        try:
            evidence = self.report["foreground"] = dict(
                saw_logical_pause=False, saw_backend_pause=False, observations=[]
            )

            def observe(event, value):
                if event not in ("session", "progress"):
                    return
                status = self.state()
                evidence["saw_logical_pause"] |= registry.local_final_active() and self.entry.final_paused
                evidence["saw_backend_pause"] |= status.get("backend_state", status["state"]) == "paused"
                if len(evidence["observations"]) < 8:
                    evidence["observations"].append(
                        dict(event=event, state=status["state"], backend=status.get("backend_state"))
                    )

            self.disk.setParms(dict(time_limit=0.8, picture=str(self.path.parent / "Foreground Disk.exr")))
            began = time.monotonic()
            with final_render.observe(observe):
                self.disk.render(frame_range=(hou.frame(), hou.frame()))
            evidence["seconds"] = time.monotonic() - began
            self.phase = "foreground_returned"
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.heartbeat.stop()
        self.report.update(
            result="failed" if error else "passed",
            error=error,
            phase=self.phase,
            jobs=[job.status() for job in self.jobs],
            elapsed_seconds=time.monotonic() - self.started,
            heartbeat=dict(
                count=len(self.beats),
                p95_seconds=float(np.percentile(self.beats, 95)) if self.beats else None,
                max_seconds=max(self.beats) if self.beats else None,
            ),
        )
        for job in self.jobs:
            if job.status()["state"] not in TERMINAL:
                job.stop()
        for panel in self.panels:
            panel.dispose()
            panel.close()
        # RemoteSession has a closed/device-byte contract, but no owned host
        # geometry byte counter. The native-retirement helper expects that field.
        from cycles_houdini.final_stream import RemoteSession

        native_sessions = []
        self.report["final_view_retirement"] = []
        for session in self.sessions:
            state = session.status()
            if not isinstance(session, RemoteSession):
                native_sessions.append(session)
            else:
                self.report["final_view_retirement"].append(state)
                if state["state"] != "closed" or state.get("device_allocation_bytes", 0) != 0:
                    error = error or "Final display session did not close: " + str(state)
                    self.report["error"] = error
        retire_and_exit(
            self.path, self.report, native_sessions, 1 if error else 0, (final_render.close_all, registry.close_all)
        )


workflow = Workflow()
