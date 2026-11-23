# SPDX-License-Identifier: Apache-2.0
"""Real viewer mode edits, multipart Save and three-frame clapper output."""

import json
import os
from pathlib import Path
import time
import traceback
from unittest.mock import patch
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import output, passes, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel
from multipart_fixture import verify_parts


class Workflow:
    def __init__(self):
        self.report_path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.dest = self.report_path.parent
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.dest / "Viewer Jobs"))
        self.report = dict(result="running", checks=[])
        self.start = time.monotonic()
        self.phase = "ipr"
        self.done = self.busy = False
        geo = hou.node("/obj").createNode("geo", "multipart_ui_subject", run_init_scripts=False)
        sop = geo.createNode("box")
        sop.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "multipart_ui_camera")
        camera.parm("tz").set(5)
        self.rop = hou.node("/out").createNode("cycles_ipr", "multipart_ui_render")
        self.rop.setParms(
            dict(
                source_mode="sop",
                soppath=sop.path(),
                camera=camera.path(),
                resx=96,
                resy=64,
                samples=8,
                ipr_samples=8,
                ipr_resolution_percent=100,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                render_threads=2,
                output_multipart=1,
                output_pass_folders=1,
                output_precision="half",
                output_compression="dwab",
                picture=str(self.dest / "sequence.$F4.exr"),
                trange=1,
            )
        )
        for parm in self.rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        self.rop.parmTuple("f").set((1, 3, 1))
        set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        passes.add(
            self.rop,
            "ao",
            name="AO",
            separate=True,
            path=str(self.dest / "ignored.exr"),
            compression="dwaa",
            precision="half",
        )
        passes.add(self.rop, "crypto_object", name="CryptoZIP", levels=6, compression="zip", precision="float")
        passes.add(self.rop, "crypto_object", name="CryptoPIZ", levels=6, compression="piz", precision="float")
        self.panel = RenderPanel()
        self.panel.resize(1400, 650)
        self.panel.show()
        self.panel.set_rop(self.rop)
        self.panel.queue_render()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

    def tick(self):
        if self.done or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.start < 75, self.phase
            p = self.panel
            r = self.rop
            if self.phase == "retire":
                if self.job.is_running():
                    return
                assert self.job.poll() == 0
                self.finish()
                return
            if self.phase == "final":
                view = p._final_view
                if view is None or view.running or view.entry is None:
                    return
                assert not view.error, view.error
                assert view.entry.session.job_status()["completed_frames"] == 3
                for frame in (1, 2, 3):
                    verify_parts(self.dest / "beauty" / f"sequence.{frame:04}.exr")
                assert not (self.dest / "ignored.exr").exists()
                self.report["checks"].append(
                    "Actual clapper writes three multipart frames with independent codecs/types and grouped exact Cryptomatte"
                )
                p.grab().save(str(self.dest / "multipart-viewer.png"))
                self.job = view.job
                p.dispose()
                p.close()
                self.phase = "retire"
                return
            if not p.entry or p.entry.session.status()["state"] != "complete" or p.canvas.image.isNull():
                return
            status = p.entry.session.status()
            if self.phase == "ipr":
                self.generation = status["generation"]
                self.conversions = status["geometry_conversions"]
                r.parm("output_separate_passes").set(1)
                r.updateParmStates()
                assert r.parm("output_multipart").isDisabled() and r.evalParm("output_multipart")
                assert len(output.plan(r, hou.time())) == 4
                self.due = time.monotonic() + 0.4
                self.phase = "separate"
                return
            if time.monotonic() < self.due:
                return
            assert status["generation"] == self.generation and status["geometry_conversions"] == self.conversions
            if self.phase == "separate":
                r.setParms(dict(output_separate_passes=0, output_multipart=0))
                r.updateParmStates()
                assert not r.parm("output_multipart").isDisabled() and not r.parm("pass_path1").isDisabled()
                self.due = time.monotonic() + 0.4
                self.phase = "single"
                return
            if self.phase == "single":
                r.setParms(dict(output_multipart=1, pass_compression1="zip", pass_precision1="float"))
                r.updateParmStates()
                assert r.parm("pass_path1").isDisabled() and r.parm("pass_separate1").isDisabled()
                assert not r.parm("pass_compression1").isDisabled() and not r.parm("pass_precision1").isDisabled()
                self.due = time.monotonic() + 0.4
                self.phase = "repair"
                return
            if self.phase == "repair":
                r.setParms(dict(pass_compression1="dwaa", pass_precision1="half"))
                self.due = time.monotonic() + 0.4
                self.phase = "save"
                return
            if self.phase == "save":
                assert r.evalParm("pass_separate1") and r.evalParm("pass_path1") == str(self.dest / "ignored.exr")
                with patch("hou.ui.selectFile", return_value=str(self.dest / "manual.exr")):
                    QtTest.QTest.mouseClick(p.save_button, QtCore.Qt.LeftButton)
                self.phase = "saved"
                return
            if self.phase == "saved":
                if p._pending_save is not None:
                    return
                verify_parts(self.dest / "beauty/manual.exr")
                assert not (self.dest / "ignored.exr").exists()
                self.report["checks"].append(
                    "Live mode/codec/precision edits preserve generation and stored controls; actual viewer Save writes one multipart file"
                )
                QtTest.QTest.mouseClick(p.final_render_button, QtCore.Qt.LeftButton)
                self.phase = "final"
                return
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.done:
            return
        self.done = True
        self.timer.stop()
        self.panel.dispose()
        self.panel.close()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.report_path.write_text(json.dumps(self.report, indent=2))
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
