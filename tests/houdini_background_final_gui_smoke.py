# SPDX-License-Identifier: Apache-2.0
"""Native ROP action, keyboard parameter edits and viewport navigation during a heavy batch render."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import background_render as bg, final_render
from packed_deformation_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(
            result="running",
            scope=__doc__,
            events=[],
            heartbeat_gaps=[],
            physical_mouse_certification=False,
            prototype_triangles=1036800,
            instances=16,
        )
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "background-jobs"))
        self.fixture = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.motion.parm("test_rows").set(721)
        f.targets.setParms(dict(rows=4, cols=4))
        f.targets.parmTuple("size").set((6, 6))
        self.rop = f.rop(f.nested)
        self.rop.setParms(
            dict(
                samples=1048576,
                time_limit=1.5,
                resx=320,
                resy=180,
                trange=1,
                picture="$HIP/background-final.$F4.exr",
                forceobject=f.obj.path(),
            )
        )
        for parm in self.rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        self.rop.parmTuple("f").set((1, 5, 1))
        f.motion.parm("test_broken").setExpression("if($F==2 || $F==4,1,0)", hou.exprLanguage.Hscript)
        f.camera.parm("tx").setExpression("2*(rint($FF)-1)", hou.exprLanguage.Hscript)
        # Keep the heavy render source out of foreground viewport cooking. A
        # native display proxy lets the artist continue modelling/navigation.
        f.obj.setDisplayFlag(False)
        self.subject = hou.node("/obj").createNode("geo", "foreground_editable", run_init_scripts=False)
        box = self.subject.createNode("box")
        box.setDisplayFlag(True)
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(800, 700))
        self.settings.setCurrentNode(self.rop)
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(700, 500))
        self.scene.curViewport().homeAll()
        hou.hipFile.save(str(self.path.parent / "artist-background.hiplc"), save_to_recent_files=False)
        self.subject.parm("tz").set(0.125)  # A real unsaved artist edit.
        self.before = dict(
            scene=hou.hipFile.path(),
            frame=hou.frame(),
            hip=hou.getenv("HIP"),
            hipname=hou.getenv("HIPNAME"),
            dirty=hou.hipFile.hasUnsavedChanges(),
        )
        assert self.before["dirty"]
        self.job = None
        self.phase = "launch"
        self.finished = False
        self.count = 0
        self.next_at = 0
        self.began = self.beat = time.monotonic()
        self.busy = False
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["heartbeat_gaps"].append(now - self.beat)
            self.beat = now
            assert now - self.began < 150, self.report
            if now < self.next_at:
                return
            if self.phase == "launch":
                assert self.rop.parm("ch_render_background").isHidden() is False
                assert self.rop.parmTemplateGroup().find("executebackground").isHidden()
                assert self.rop.parm("ch_background_jobs").isHidden() is False
                began = time.monotonic()
                self.rop.parm("ch_render_background").pressButton()
                self.report["launch_seconds"] = time.monotonic() - began
                self.job = bg.last_job(self.rop)
                assert (
                    dict(
                        scene=hou.hipFile.path(),
                        frame=hou.frame(),
                        hip=hou.getenv("HIP"),
                        hipname=hou.getenv("HIPNAME"),
                        dirty=hou.hipFile.hasUnsavedChanges(),
                    )
                    == self.before
                )
                assert (
                    not final_render.active_sequences() and self.rop.cachedUserData(final_render._summary_key) is None
                )
                self.phase = "wait"
                return
            status = self.job.status()
            self.report["job"] = status
            assert status["state"] not in ("failed", "cancelled"), status
            if self.phase == "focus":
                self.settings.moveFocusTo("t")
                self.phase = "keys"
                self.next_at = now + 0.12
                return
            if self.phase == "keys":
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, str(self.value))
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = "confirm"
                self.next_at = now + 0.12
                return
            if self.phase == "confirm":
                assert abs(self.subject.evalParm("tx") - self.value) < 0.00001
                assert status["state"] == "rendering", status
                viewport = self.scene.curViewport()
                camera = viewport.defaultCamera()
                before = camera.translation()
                camera.setTranslation(hou.Vector3(before) + hou.Vector3(0.05, 0, 0))
                viewport.setDefaultCamera(camera)
                assert viewport.defaultCamera().translation() != before
                self.report["events"].append(
                    dict(
                        tx=self.value,
                        child_state=status["state"],
                        child_frame=status.get("frame"),
                        keyboard_edit=True,
                        viewport_navigated=True,
                    )
                )
                self.phase = "wait"
            if status["state"] == "rendering" and self.count < 5:
                self.count += 1
                self.value = self.count * 0.1
                self.settings.setCurrentNode(self.subject)
                self.settings.setIsCurrentTab()
                self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
                self.window.show()
                self.window.raise_()
                self.window.activateWindow()
                self.window.setFocus()
                QtGui.QCursor.setPos(self.window.mapToGlobal(self.window.rect().center()))
                self.phase = "focus"
                self.next_at = now + 0.12
                return
            if status["state"] == "completed" and self.job.poll() is not None:
                assert len(self.report["events"]) == 5, self.report["events"]
                assert (
                    status["pid"] != os.getpid() and status["completed_frames"] == 5 and status["created_sessions"] == 1
                )
                assert (
                    not final_render.active_sequences() and self.rop.cachedUserData(final_render._summary_key) is None
                )
                records = [json.loads(line) for line in (self.job.directory / "frames.jsonl").read_text().splitlines()]
                assert [record["frame"] for record in records] == [1, 2, 3, 4, 5]
                for frame, record in enumerate(records, 1):
                    assert Path(record["picture"]).is_file()
                    metadata = record["status"]
                    assert metadata["geometry_resources"] == 1 and metadata["object_resources"] == 16, metadata
                    assert bool(metadata["source_diagnostics"]) == (frame in (2, 4)), metadata
                gaps = sorted(self.report["heartbeat_gaps"][2:])
                self.report.update(
                    heartbeat_max_seconds=max(gaps),
                    heartbeat_p95_seconds=gaps[int((len(gaps) - 1) * 0.95)],
                    no_foreground_render_session=True,
                    outputs=[record["picture"] for record in records],
                )
                assert self.report["launch_seconds"] < 2 and max(gaps) < 1, self.report
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
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        # Leave the Qt timeout callback before destroying native floating panes
        # or exiting Houdini. A failed fixture must also shut down cleanly.
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        if self.job and self.job.poll() is None:
            self.job.terminate()
            self.job.wait(timeout=15)
        self.settings.close()
        self.scene.close()
        self.timer.deleteLater()
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
