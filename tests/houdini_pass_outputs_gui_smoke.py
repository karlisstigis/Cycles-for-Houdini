# SPDX-License-Identifier: Apache-2.0
"""Native pass pane, live type repairs, asynchronous Save and clapper sequence."""

import json
import os
from pathlib import Path
import time
import traceback
from unittest.mock import patch
import hou
import OpenImageIO as oiio
from PySide6 import QtCore, QtTest
from cycles_houdini import passes, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.dest = self.path.parent
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.dest / "Viewer Jobs"))
        self.report = dict(result="running", checks=[])
        self.began = time.monotonic()
        self.phase = "ipr"
        self.finished = self.busy = False
        geo = hou.node("/obj").createNode("geo", "pass_ui_subject", run_init_scripts=False)
        sop = geo.createNode("box")
        sop.setRenderFlag(True)
        cam = hou.node("/obj").createNode("cam", "pass_ui_camera")
        cam.parm("tz").set(5)
        self.rop = hou.node("/out").createNode("cycles_ipr", "pass_ui_render")
        self.rop.setParms(
            dict(
                source_mode="sop",
                soppath=sop.path(),
                camera=cam.path(),
                resx=96,
                resy=64,
                samples=16,
                ipr_samples=8,
                ipr_resolution_percent=100,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                render_threads=2,
                output_precision="half",
                output_compression="dwab",
                picture=str(self.dest / "finalbeauty.$F4.exr"),
                trange=1,
                persistent_data=1,
            )
        )
        for parm in self.rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        self.rop.parmTuple("f").set((1, 3, 1))
        set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.index = passes.add(
            self.rop, "crypto_object", name="Masks", compression="zip", path=str(self.dest / "finalMasks.$F4.exr")
        )
        passes.add(self.rop, "ao", compression="dwab", precision="half")
        self.rop.parm("output_separate_passes").set(1)
        self.pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(1000, 1000))
        self.pane.setCurrentNode(self.rop)
        self.pane.setPin(True)
        target = self.rop.parm("pass_compression1")
        for folder, index in zip(target.containingFolderSetParmTuples(), target.containingFolderIndices()):
            template = self.rop.parmTemplateGroup().find(folder.name())
            folder[0].set(
                1
                if isinstance(template, hou.FolderParmTemplate) and template.folderType() == hou.folderType.Collapsible
                else index
            )
        self.panel = RenderPanel()
        self.panel.resize(1400, 650)
        self.panel.show()
        self.panel.set_rop(self.rop)
        self.panel.queue_render()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 70, self.phase
            p = self.panel
            if self.phase == "retired":
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
                status = view.entry.session.job_status()
                assert status["completed_frames"] == 3, status
                for frame in (1, 2, 3):
                    for stem, codec in (("finalbeauty", "dwab"), ("finalMasks", "piz")):
                        target = (
                            self.dest / "beauty" / f"{stem}.{frame:04}.exr"
                            if stem == "finalbeauty"
                            else self.dest / f"{stem}.{frame:04}.exr"
                        )
                        image = oiio.ImageInput.open(str(target))
                        assert image, oiio.geterror()
                        spec = image.spec()
                        assert spec.get_string_attribute("compression") == codec
                        if stem == "finalbeauty":
                            assert set(spec.channelnames) == {"R", "G", "B", "A"}
                        else:
                            assert "Masks00.R" in spec.channelnames
                        image.close()
                    image = oiio.ImageInput.open(str(self.dest / "ao" / f"finalbeauty.ao.{frame:04}.exr"))
                    assert image, oiio.geterror()
                    assert image.spec().get_string_attribute("compression") == "dwab"
                    assert set(image.spec().channelnames) == {"R", "G", "B"}
                    image.close()
                    image = oiio.ImageInput.open(str(self.dest / "ao" / f"finalbeauty.ao.{frame:04}.exr"))
                    spec = image.spec()
                    assert str(spec.format) == "half" or all(str(f) == "half" for f in spec.channelformats)
                    image.close()
                p.grab().save(str(self.dest / "viewer-separate-outputs.png"))
                expansion = self.rop.parm("pass_output1")
                if expansion is not None:
                    expansion.set(1)
                QtTest.QTest.qWait(100)
                window = hou.qt.floatingPanelWindow(self.pane.floatingPanel())
                window.grab().save(str(self.dest / "native-pass-list.png"))
                self.report["checks"].append(
                    "Actual clapper renders three frames to Beauty DWAB, explicit AO DWAB and Cryptomatte PIZ in the viewer"
                )
                self.job = view.job
                p.dispose()
                p.close()
                self.phase = "retired"
                return
            if not p.entry or p.entry.session.status()["state"] != "complete" or p.canvas.image.isNull():
                return
            status = p.entry.session.status()
            if self.phase == "ipr":
                self.generation = status["generation"]
                self.conversions = status["geometry_conversions"]
                self.rop.setParms(
                    dict(
                        pass_compression1="piz",
                        pass_compression2="zip",
                        pass_precision2="float",
                        pass_name1="Masks",
                        output_pass_folders=1,
                        output_separate_passes=0,
                    )
                )
                self.rop.updateParmStates()
                assert (
                    not self.rop.parm("pass_precision1").isDisabled()
                    and not self.rop.parm("pass_precision2").isDisabled()
                )
                self.due = time.monotonic() + 0.4
                self.phase = "file_only"
                return
            if self.phase == "file_only":
                if time.monotonic() < self.due:
                    return
                assert status["generation"] == self.generation, "Encoding edits restarted rendering"
                if not self.rop.evalParm("output_separate_passes"):
                    self.rop.parm("output_separate_passes").set(1)
                    self.due = time.monotonic() + 0.4
                    return
                if self.rop.evalParm("pass_compression2") != "dwab":
                    self.rop.setParms(dict(pass_compression2="dwab", pass_precision2="half"))
                    self.due = time.monotonic() + 0.4
                    return
                assert not self.rop.evalParm("pass_separate1")
                self.report["checks"].append(
                    "Global separation, Pass Folders and per-pass encoding/routing edits do not restart IPR"
                )
                self.manual = self.dest / "manual.exr"
                with patch("hou.ui.selectFile", return_value=str(self.manual)):
                    QtTest.QTest.mouseClick(p.save_button, QtCore.Qt.LeftButton)
                self.phase = "saved"
                return
            if self.phase == "saved":
                if p._pending_save is not None:
                    return
                assert (self.dest / "beauty" / self.manual.name).is_file() and (
                    self.dest / "finalMasks.0001.exr"
                ).is_file(), p.info.text()
                image = oiio.ImageInput.open(str(self.dest / "ao/manual.ao.exr"))
                assert image, oiio.geterror()
                spec = image.spec()
                assert spec.get_string_attribute("compression") == "dwab"
                assert set(spec.channelnames) == {"R", "G", "B"}
                assert str(spec.format) == "half" or all(str(f) == "half" for f in spec.channelformats)
                image.close()
                assert status["generation"] == self.generation
                self.report["checks"].append(
                    "Actual viewer Save asynchronously writes the selected main and separate files"
                )
                self.rop.parm("pass_type1").set("normal")
                self.phase = "normal"
                return
            if self.phase == "normal":
                if status["generation"] <= self.generation:
                    return
                info = p.entry.session.frame_info()
                if not info or "normal" not in info["enabled_passes"]:
                    return
                assert "CryptoObject00" not in info["enabled_passes"]
                self.generation = status["generation"]
                self.rop.parm("pass_type1").set("crypto_object")
                self.phase = "repair"
                return
            if self.phase == "repair":
                if status["generation"] <= self.generation:
                    return
                info = p.entry.session.frame_info()
                if not info or "CryptoObject00" not in info["enabled_passes"]:
                    return
                assert status["geometry_conversions"] == self.conversions
                self.report["checks"].append(
                    "Live Cryptomatte-to-Normal and repair update passes without geometry conversion"
                )
                QtTest.QTest.mouseClick(p.final_render_button, QtCore.Qt.LeftButton)
                self.phase = "final"
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
        self.panel.dispose()
        self.panel.close()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
