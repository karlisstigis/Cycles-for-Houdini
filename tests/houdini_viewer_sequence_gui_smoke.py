# SPDX-License-Identifier: Apache-2.0
"""Actual clapper/Stop: ordered animated output, session rotation and repair."""

import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
from PySide6 import QtCore, QtTest
from cycles_houdini import final_render, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel
from cycles_houdini.viewer_timing import duration, summary as status_summary


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "Sequence Jobs"))
        self.report = dict(result="running", checks=[], runs=[])
        self.began = time.monotonic()
        self.phase = "ipr"
        self.busy = self.finished = False
        self.jobs = []
        self.beats = []
        self.last_beat = time.monotonic()
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start(10)
        geo = hou.node("/obj").createNode("geo", "sequence_subject", run_init_scripts=False)
        sop = geo.createNode("box")
        sop.setRenderFlag(True)
        cam = hou.node("/obj").createNode("cam", "sequence_camera")
        pass_fixture.set_parameters(cam, dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "sequence_render")
        pass_fixture.set_parameters(
            self.rop,
            dict(
                vobject=geo.path(),
                camera=cam.path(),
                resx=128,
                resy=96,
                samples=1048576,
                time_limit=1,
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
                trange=1,
                persistent_data=0,
                pass_depth=1,
            ),
        )
        for parm in self.rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        self.rop.parmTuple("f").set((2, 6, 2))
        self.rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
        self.rop.parmTuple("basecolor")[0].setExpression("$F/10", hou.exprLanguage.Hscript)
        set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        hou.setFrame(11)
        self.panel = RenderPanel()
        self.panel.resize(1400, 650)
        self.panel.show()
        self.panel.set_rop(self.rop)
        self.panel.queue_render()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

    def beat(self):
        now = time.monotonic()
        self.beats.append(now - self.last_beat)
        self.last_beat = now

    def start(self, name, persistent=0):
        self.name = name
        self.seen = {}
        self.images = {}
        self.phase = "sequence"
        pass_fixture.set_parameters(
            self.rop, dict(picture=str(self.path.parent / (name + ".$F4.exr")), persistent_data=persistent)
        )
        QtTest.QTest.mouseClick(self.panel.final_render_button, QtCore.Qt.LeftButton)

    def file(self, frame):
        return self.path.parent / f"{self.name}.{frame:04d}.exr"

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 100, self.phase
            p = self.panel
            if self.phase == "ipr":
                if not p.entry or p.entry.session.status()["state"] != "complete" or p.canvas.image.isNull():
                    return
                self.start("fresh")
                return
            if self.phase == "retired":
                if any(job.is_running() for job in self.jobs):
                    return
                self.report["child_exit_codes"] = [job.poll() for job in self.jobs]
                assert self.report["child_exit_codes"] == [0, 0, 2, 0], self.report
                self.finish()
                return
            view = p._final_view
            if view and view.job and view.job not in self.jobs:
                self.jobs.append(view.job)
            assert hou.frame() == 11, "Batch must not move the artist timeline"
            assert not final_render.active_sequences() and not p._in_render
            if view is None or view.entry is None:
                return
            session = view.entry.session
            job = session.job_status()
            status = session.status()
            if view.running and job.get("frame") is not None:
                frame = round(job["frame"])
                self.seen[frame] = status["generation"]
                info = session.frame_info()
                if (
                    info
                    and p.canvas.generation == status["generation"]
                    and p._sequence == info["sequence"]
                    and not p.canvas.image.isNull()
                ):
                    self.images[2 * status["generation"]] = p.canvas.image.pixelColor(
                        p.canvas.image.width() // 2, p.canvas.image.height() // 2
                    ).name()
                if not session.stop_requested:
                    assert p.stop_button.isEnabled()
                assert not p.final_render_button.isEnabled()
            if self.phase == "sequence":
                if view.running:
                    return
                assert not view.error, view.error
                if status["generation"] != 3 or p.canvas.generation != status["generation"]:
                    return
                self.images[6] = p.canvas.image.pixelColor(
                    p.canvas.image.width() // 2, p.canvas.image.height() // 2
                ).name()
                assert "Frame 6 (3/3)" in p.info.text(), p.info.text()
                assert p.canvas.generation == status["generation"] and not p.canvas.image.isNull()
                summary = self.rop.cachedUserData(final_render._summary_key)
                assert summary["completed_frames"] == 3 and summary["last_frame"] == 6, summary
                assert summary["created_sessions"] == (1 if self.name == "persistent" else 3), summary
                assert job["total_frames"] == 3 and job["frame_index"] == 3, job
                records = [json.loads(line) for line in (view.job.directory / "frames.jsonl").read_text().splitlines()]
                assert [record["frame"] for record in records] == [2, 4, 6], records
                assert all(record["frame_seconds"] > 0 for record in records)
                assert records[0]["previous_frame_seconds"] is None
                for previous, current in zip(records, records[1:]):
                    assert current["previous_frame_seconds"] == previous["frame_seconds"], records
                assert job["frame_started_monotonic"] is None
                assert job["frame_elapsed_seconds"] == records[-1]["frame_seconds"]
                assert job["previous_frame_seconds"] == records[-2]["frame_seconds"]
                headline = status_summary(p.info.text())
                assert "Time: " + duration(records[-1]["frame_seconds"]) in headline, headline
                assert "Last: " + duration(records[-2]["frame_seconds"]) in headline, headline
                assert "Frame 6 (3/3)" in headline, headline
                for frame in (2, 4, 6):
                    image = oiio.ImageBuf(str(self.file(frame)))
                    pixels = image.get_pixels()
                    assert pixels is not None, image.geterror()
                    names = list(image.spec().channelnames)
                    np.testing.assert_allclose(
                        pixels[48, 64, [names.index(c) for c in ("R", "G", "B")]],
                        (frame / 10, 0.4, 0.8),
                        atol=0.002,
                        rtol=0,
                    )
                if self.name != "repaired":
                    assert set(self.seen) == {2, 4, 6}, self.seen
                    assert len(set(self.images.values())) == 3, self.images
                assert p.save_button.isEnabled()
                self.report["runs"].append(
                    dict(name=self.name, summary=summary, displayed_colors=self.images, frame_timings=records)
                )
                self.report["checks"].append(self.name + " range 2/4/6, animated pixels and held last frame")
                self.report["checks"].append(
                    self.name + " frame timers reset, retain previous completed time and freeze at completion"
                )
                if self.name == "fresh":
                    self.start("persistent", 1)
                    return
                if self.name == "persistent":
                    pass_fixture.parm(self.rop, "time_limit").setExpression(
                        "if($F == 2, 0.5, 10)", hou.exprLanguage.Hscript
                    )
                    self.start("cancelled")
                    self.phase = "wait_second"
                    return
                p.grab().save(str(self.path.parent / "sequence-viewer.png"))
                p.dispose()
                p.close()
                self.phase = "retired"
                return
            if self.phase == "wait_second":
                if job.get("completed_frames") != 1 or job.get("frame") != 4 or status["state"] != "rendering":
                    return
                QtTest.QTest.mouseClick(p.pause_button, QtCore.Qt.LeftButton)
                self.phase = "paused"
                return
            if self.phase == "paused":
                if status["state"] != "paused":
                    return
                assert p.entry.paused
                assert self.file(2).is_file() and not self.file(4).exists()
                self.paused_at = time.monotonic()
                self.phase = "resume"
                return
            if self.phase == "resume":
                if time.monotonic() - self.paused_at < 0.2:
                    return
                QtTest.QTest.mouseClick(p.pause_button, QtCore.Qt.LeftButton)
                self.phase = "stop"
                return
            if self.phase == "stop":
                if status["state"] != "rendering":
                    return
                self.stop_at = time.monotonic()
                QtTest.QTest.mouseClick(p.stop_button, QtCore.Qt.LeftButton)
                self.phase = "cancelled"
                return
            if self.phase == "cancelled":
                if view.running:
                    return
                summary = self.rop.cachedUserData(final_render._summary_key)
                assert summary["cancelled"] and summary["completed_frames"] == 1, summary
                assert self.file(2).is_file() and not self.file(4).exists() and not self.file(6).exists()
                self.report["stop_seconds"] = time.monotonic() - self.stop_at
                assert self.report["stop_seconds"] < 5, self.report
                self.report["checks"].append("Pause/resume on frame 4; Stop preserves frame 2 and cancels frames 4/6")
                pass_fixture.parm(self.rop, "time_limit").deleteAllKeyframes()
                pass_fixture.parm(self.rop, "time_limit").set(0.3)
                self.start("repaired")
                return
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
        self.report.update(
            result="failed" if error else "passed",
            error=error,
            heartbeat=dict(
                p95_seconds=float(np.percentile(self.beats, 95)), max_seconds=max(self.beats), count=len(self.beats)
            ),
        )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
