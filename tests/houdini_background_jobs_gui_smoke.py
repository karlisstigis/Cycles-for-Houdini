# SPDX-License-Identifier: Apache-2.0
"""Live project jobs: actual Cancel/Force Stop controls, paging and HIP storage."""

import json, os, subprocess, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import background_render as bg, background_panel, device_selection, rop_controls


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[], physical_mouse_certification=False)
        self.began = time.monotonic()
        self.busy = self.finished = False
        self.phase = "launch"
        self.job = self.blocked = None
        project = self.path.parent / "Project With Spaces"
        project.mkdir()
        self.root = project / "cycles_background"
        self.root.mkdir()
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", "")
        for i in range(57):
            folder = self.root / f"20000101T000000-{i:04}"
            folder.mkdir()
            bg._write(
                folder / "status.json",
                dict(
                    schema=1,
                    id=folder.name,
                    state="completed",
                    created=1,
                    rop=f"/out/history_{i:03}",
                    completed_frames=5,
                    skipped_frames=0,
                    error="",
                ),
            )
        geo = hou.node("/obj").createNode("geo", "job_subject", run_init_scripts=False)
        box = geo.createNode("box")
        box.setDisplayFlag(True)
        box.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "job_camera")
        camera.parm("tz").set(5)
        self.rop = hou.node("/out").createNode("cycles_ipr", "job_controls")
        self.rop.setParms(
            dict(
                vobject=geo.path(),
                camera=camera.path(),
                resx=128,
                resy=96,
                samples=1048576,
                time_limit=30,
                use_denoise=0,
                adaptive_sampling=0,
                render_threads=2,
                allowmotionblur=0,
                use_material_assignments=0,
                material_kind="emission",
                emission_strength=1,
                trange=0,
                picture="$HIP/never-written.$F4.exr",
            )
        )
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        hou.hipFile.save(str(project / "artist.hiplc"), save_to_recent_files=False)
        assert bg._root() == self.root.resolve()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

    def action(self, name):
        self.rop.parm("ch_controls").pressButton()
        menu = rop_controls._controls_menu
        action = next(a for a in menu.actions() if a.data() == name)
        menu.close()
        action.trigger()

    def click(self, button):
        assert button.isEnabled() and button.isVisible(), button.text()
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)

    def select(self, job):
        self.manager.table.selectRow(next(i for i, j in enumerate(self.manager.jobs) if j.key == job.key))

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 100, self.phase
            if self.phase == "launch":
                self.action("render_background")
                self.job = bg.last_job(self.rop)
                assert self.job.directory.parent == self.root.resolve()
                self.action("background_jobs")
                self.manager = background_panel._window
                assert self.manager.isVisible() and self.manager.timer.isActive()
                self.phase = "rendering"
                return
            if self.phase == "rendering":
                status = self.job.status()
                assert status["state"] not in bg._terminal, status
                if not status.get("generation"):
                    return
                self.manager.refresh()
                assert self.manager.table.rowCount() <= 51
                self.click(self.manager.older_button)
                assert any(v["rop"] == "/out/history_000" for v in self.manager.values.values())
                self.click(self.manager.newest_button)
                self.select(self.job)
                self.click(self.manager.cancel_button)
                assert (self.job.directory / "stop").is_file() and not self.manager.cancel_button.isEnabled()
                self.phase = "stopping"
                return
            if self.phase == "stopping":
                status = self.job.status()
                if status["state"] != "cancelled" or self.job.poll() is None:
                    return
                assert status["completed_frames"] == 0 and not list(self.root.parent.glob("never-written.*.exr"))
                self.report["events"].append(dict(action="Live window Cancel", status=status))
                folder = self.root / "20990101T000000-blocked"
                folder.mkdir()
                bg._write(
                    folder / "status.json",
                    dict(
                        schema=1,
                        id=folder.name,
                        state="rendering",
                        created=time.time(),
                        rop="/out/blocked",
                        completed_frames=0,
                        skipped_frames=0,
                        error="",
                    ),
                )
                process = subprocess.Popen(
                    [str(Path(hou.getenv("HFS")) / "python313/python.exe"), "-c", "import time; time.sleep(120)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.blocked = bg._registry.adopt(folder, process)
                self.manager.refresh()
                self.select(self.blocked)
                self.click(self.manager.cancel_button)
                assert self.manager.force_button.isVisible()
                self.click(self.manager.force_button)
                self.phase = "forced"
                return
            if self.phase == "forced":
                if self.blocked.poll() is None:
                    return
                status = self.blocked.status()
                assert status["state"] == "cancelled" and status["forced"], status
                self.report["events"].append(dict(action="Live window Force Stop", status=status))
                project = self.path.parent / "Second Project"
                project.mkdir()
                hou.hipFile.save(str(project / "artist.hiplc"), save_to_recent_files=False)
                self.manager.update_jobs()
                assert self.manager.root == project / "cycles_background"
                assert self.manager.table.rowCount() == 0 and not self.manager.cancel_button.isEnabled()
                self.manager.close()
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
        for job in (self.job, self.blocked):
            if job and job.poll() is None:
                job.stop()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
