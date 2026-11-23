# SPDX-License-Identifier: Apache-2.0
"""Actual transport buttons, native final output, retained display and IPR return."""

import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
import psutil
from PySide6 import QtCore, QtTest
from cycles_houdini import final_render, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "Viewer Jobs With Spaces"))
        self.report = dict(result="running", checks=[])
        self.began = time.monotonic()
        self.phase = "ipr"
        self.busy = self.finished = False
        self.jobs = []
        self.beats = []
        self.final_beats = []
        self.last_beat = time.monotonic()
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start(10)
        geo = hou.node("/obj").createNode("geo", "viewer_final_subject", run_init_scripts=False)
        box = geo.createNode("box")
        box.setRenderFlag(True)
        cam = hou.node("/obj").createNode("cam", "viewer_final_camera")
        pass_fixture.set_parameters(cam, dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "viewer_final_render")
        self.picture = self.path.parent / "Viewer Final.exr"
        pass_fixture.set_parameters(
            self.rop,
            dict(
                vobject=geo.path(),
                camera=cam.path(),
                resx=128,
                resy=96,
                samples=128,
                ipr_samples=4,
                ipr_resolution_percent=50,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                use_material_assignments=0,
                material_kind="emission",
                emission_strength=1,
                render_threads=2,
                picture=str(self.picture),
                trange=0,
                persistent_data=0,
                pass_depth=1,
            ),
        )
        for parm in self.rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        self.rop.parmTuple("f").set((1, 5, 1))
        self.rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
        set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.panel = RenderPanel()
        self.panel.set_rop(self.rop)
        self.panel.resize(1400, 650)
        self.panel.show()
        self.panel.memory_details.setChecked(True)
        buttons = [
            self.panel.final_render_button,
            self.panel.render_button,
            self.panel.pause_button,
            self.panel.stop_button,
            self.panel.refresh_button,
        ]
        assert all(not b.icon().isNull() and not b.text() for b in buttons)
        assert all(b.height() == self.panel.render_button.height() for b in buttons)
        assert all(b.width() == b.height() for b in buttons)
        assert self.panel.render_button.parentWidget().property("cycles_cluster") == "transport"
        self.panel.queue_render()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

    def beat(self):
        now = time.monotonic()
        interval = now - self.last_beat
        self.beats.append(interval)
        self.last_beat = now
        view = getattr(getattr(self, "panel", None), "_final_view", None)
        if (
            view
            and view.running
            and view.entry
            and not self.panel._in_render
            and view.entry.session.status()["generation"]
        ):
            self.final_beats.append(interval)

    def start_final(self, phase):
        self.phase = phase
        QtTest.QTest.mouseClick(self.panel.final_render_button, QtCore.Qt.LeftButton)

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 90, self.phase
            p = self.panel
            if self.phase == "ipr":
                if not p.entry or p.entry.session.status()["state"] != "complete" or p.canvas.image.isNull():
                    return
                assert p.canvas.full_size == (64, 48), p.canvas.full_size
                self.start_final("final")
                return
            view = p._final_view
            if view is not None and view.job is not None and view.job not in self.jobs:
                self.jobs.append(view.job)
            if view is not None and view.running:
                assert not p._in_render and not final_render.active_sequences()
                for child in psutil.Process().children(recursive=True):
                    try:
                        assert child.name() != "idialog.exe"
                    except psutil.NoSuchProcess:
                        pass
            if self.phase == "pause_render":
                if (
                    view is None
                    or not view.entry
                    or view.entry.session.status()["state"] != "rendering"
                    or not view.entry.session.status()["generation"]
                ):
                    return
                assert p.stop_button.isEnabled() and p.pause_button.isEnabled()
                QtTest.QTest.mouseClick(p.pause_button, QtCore.Qt.LeftButton)
                self.phase = "paused"
                return
            if self.phase == "paused":
                if view.entry.session.status()["state"] != "paused":
                    return
                assert p.entry.paused and p.pause_button.isChecked()
                self.paused_at = time.monotonic()
                self.phase = "resume"
                return
            if self.phase == "resume":
                if time.monotonic() - self.paused_at < 0.2:
                    return
                assert len(self.beats) > 10
                # Host object/viewport changes remain independent of the batch snapshot.
                pass_fixture.parm(hou.node("/obj/viewer_final_subject"), "ty").set(0.1)
                pass_fixture.parm(hou.node("/obj/viewer_final_subject"), "ty").set(0)
                QtTest.QTest.mouseClick(p.pause_button, QtCore.Qt.LeftButton)
                self.phase = "stop"
                return
            if self.phase == "stop":
                if view.entry.session.status()["state"] != "rendering":
                    return
                assert not p.entry.paused
                self.stop_at = time.monotonic()
                QtTest.QTest.mouseClick(p.stop_button, QtCore.Qt.LeftButton)
                self.phase = "cancelled"
                return
            if self.phase in ("final", "cancelled", "output_failed", "repaired"):
                if view is None or view.running:
                    return
                if self.phase == "cancelled":
                    assert view.error and self.rop.cachedUserData(final_render._summary_key)["cancelled"]
                    assert not self.cancel_picture.exists()
                    latency = time.monotonic() - self.stop_at
                    assert latency < 5, latency
                    self.report["stop_seconds"] = latency
                    self.report["checks"].append("final cancellation preserves output")
                    blocked = self.path.parent / "Blocked.exr"
                    blocked.mkdir()
                    pass_fixture.set_parameters(self.rop, dict(samples=8, picture=str(blocked)))
                    self.start_final("output_failed")
                    return
                if self.phase == "output_failed":
                    assert view.error and self.rop.cachedUserData(final_render._summary_key)["error"]
                    self.report["checks"].append("output failure stays explicit")
                    pass_fixture.set_parameters(
                        self.rop, dict(samples=8, picture=str(self.path.parent / "Repaired Final.exr"))
                    )
                    self.start_final("repaired")
                    return
                if p.canvas.image.isNull() or p.canvas.full_size != (128, 96):
                    return
                assert view.owned and view.entry is p.entry
                assert p.mode_menu.currentText() == "Final"
                assert p.preview_resolution.currentText() == "100%"
                assert not view.error, view.error
                status = p.entry.session.status()
                assert status["state"] == "complete" and status["rendering"]["samples"] == pass_fixture.evaluate(
                    self.rop, "samples"
                ), status
                memory = p.entry.session.memory_snapshot()
                if memory is None:
                    return
                assert memory["pid"] == view.job.status()["pid"] and memory["pid"] != os.getpid(), memory
                self.report["renderer_memory"] = memory
                summary = self.rop.cachedUserData(final_render._summary_key)
                assert summary["completed_frames"] == 1 and not summary["session_alive"], summary
                assert not final_render.active_sequences()
                image = oiio.ImageBuf(pass_fixture.evaluate(self.rop, "picture"))
                pixels = image.get_pixels()
                assert pixels is not None, image.geterror()
                names = list(image.spec().channelnames)
                expected = (0.2, 0.4, 0.8) if self.phase == "final" else (0.3, 0.4, 0.8)
                np.testing.assert_allclose(
                    pixels[48, 64, [names.index(c) for c in ("R", "G", "B")]], expected, atol=0.002, rtol=0
                )
                self.report["checks"].append(self.phase + " native output and retained image")
                if self.phase == "repaired":
                    self.repaired_job = view.job
                    self.startup_picture = self.path.parent / "Stopped During Startup.exr"
                    pass_fixture.set_parameters(self.rop, dict(picture=str(self.startup_picture)))
                    self.start_final("startup")
                    return
                self.generation = status["generation"]
                self.image = p.canvas.image.copy()
                p.pass_menu.setCurrentIndex(p.pass_menu.findData("depth"))
                self.phase = "depth"
                return
            if self.phase == "depth":
                frame = p.entry.session.frame_info()
                if not frame or "depth" not in frame["passes"] or p._pass_color or p._settings_dirty:
                    return
                assert p.entry.session.status()["generation"] == self.generation
                # Save writes beside the ROP's Output Picture with a time stamp
                # (no file dialog); take the name the viewer chose.
                QtTest.QTest.mouseClick(p.save_button, QtCore.Qt.LeftButton)
                assert p._pending_save is not None, p.info.text()
                self.extra = Path(p._pending_save["filename"])
                assert self.extra.parent == Path(pass_fixture.evaluate(self.rop, "picture")).absolute().parent
                self.phase = "saved"
                return
            if self.phase == "saved":
                if p._pending_save is not None:
                    return
                assert self.extra.is_file(), p.info.text()
                image = oiio.ImageBuf(str(self.extra))
                assert image.get_pixels() is not None, image.geterror()
                assert p.entry.session.status()["generation"] == self.generation
                self.report["checks"].append("held final AOV selection and asynchronous additional EXR save")
                p.pass_menu.setCurrentIndex(p.pass_menu.findData("combined"))
                self.phase = "restore"
                return
            if self.phase == "restore":
                if not p._pass_color or p._settings_dirty or p.canvas.image != self.image:
                    return
                assert p.grab().save(str(self.path.parent / "final-viewer.png"))
                self.rop.parmTuple("basecolor").set((0.3, 0.4, 0.8))
                self.wait_until = time.monotonic() + 0.35
                self.phase = "hold"
                return
            if self.phase == "hold":
                if time.monotonic() < self.wait_until:
                    return
                assert p.entry.session.status()["generation"] == self.generation
                assert p.canvas.image == self.image
                assert not p.refresh_button.isEnabled() and not p.focus_pick.isEnabled()
                self.final_session = p.entry.session
                assert p.render_button.isEnabled(), p.info.text()
                QtTest.QTest.mouseClick(p.render_button, QtCore.Qt.LeftButton)
                self.phase = "return"
                return
            if self.phase == "return":
                if p._final_view is not None or not p.entry or p.entry.session.status()["state"] != "complete":
                    return
                assert self.final_session.status()["state"] == "closed"
                assert p.mode_menu.currentText() == "Interactive"
                assert p.preview_resolution.currentText() == "50%"
                self.report["checks"].append("scene edits hold final; Play releases final and resumes IPR")
                self.cancel_picture = self.path.parent / "Cancelled Final.exr"
                pass_fixture.set_parameters(
                    self.rop, dict(samples=1048576, time_limit=15, picture=str(self.cancel_picture))
                )
                self.start_final("pause_render")
                return
            if self.phase == "startup":
                if view is None or view.job is None or view.job is self.repaired_job or not view.running:
                    return
                self.startup_stop_at = time.monotonic()
                QtTest.QTest.mouseClick(p.stop_button, QtCore.Qt.LeftButton)
                self.phase = "startup_cancel"
                return
            if self.phase == "startup_cancel":
                if view.running:
                    return
                assert view.entry.session.job_status()["state"] == "cancelled"
                assert not self.startup_picture.exists()
                self.report["startup_stop_seconds"] = time.monotonic() - self.startup_stop_at
                assert self.report["startup_stop_seconds"] < 5, self.report
                session = p.entry.session
                p.dispose()
                p.close()
                assert session.status()["state"] == "closed"
                self.report["checks"].append("Stop during startup is bounded and does not write output")
                self.phase = "retired"
                return
            if self.phase == "retired":
                if any(job.is_running() for job in self.jobs):
                    return
                self.report["child_exit_codes"] = [job.poll() for job in self.jobs]
                assert all(job.poll() in (0, 1, 2) for job in self.jobs), self.report
                assert len(self.final_beats) > 10 and float(np.percentile(self.final_beats, 95)) < 0.15, (
                    self.final_beats
                )
                self.finish()
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
        self.panel.dispose()
        self.panel.close()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.report["heartbeat"] = dict(
            p95_seconds=float(np.percentile(self.beats, 95)), max_seconds=max(self.beats), count=len(self.beats)
        )
        if self.final_beats:
            self.report["while_rendering"] = dict(
                p95_seconds=float(np.percentile(self.final_beats, 95)),
                max_seconds=max(self.final_beats),
                count=len(self.final_beats),
            )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
