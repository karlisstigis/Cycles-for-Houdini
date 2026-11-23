# SPDX-License-Identifier: Apache-2.0
"""Actual ROP actions, foreground/background EXRs, job menu and native controls."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
from PySide6 import QtCore, QtWidgets
import cycles_houdini
from cycles_houdini import background_render as bg, background_panel, device_selection, final_render, rop_controls


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[], dialogs=[], physical_mouse_certification=False)
        self.began = time.monotonic()
        self.finished = self.busy = False
        self.phase = "foreground"
        self.host = Path(cycles_houdini.__file__).resolve().parents[2]
        expected = os.environ.get("CYCLES_HOUDINI_ACTIONS_EXPECTED_HOST")
        if expected:
            assert self.host == Path(expected).resolve(), (self.host, expected)
        if os.environ.get("CYCLES_HOUDINI_ACTIONS_REQUIRE_SPACES"):
            assert " " in str(self.host)
        self.report["host"] = str(self.host)
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "Background Jobs With Spaces"))
        geo = hou.node("/obj").createNode("geo", "rop_button_subject", run_init_scripts=False)
        box = geo.createNode("box")
        box.setRenderFlag(True)
        box.setDisplayFlag(True)
        camera = hou.node("/obj").createNode("cam", "rop_button_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "rop_button_render")
        for name in ("renderdialog", "ch_render_background", "ch_background_jobs", "ch_cancel_render"):
            assert self.rop.parm(name).parmTemplate().isHidden(), name
        assert not self.rop.parm("ch_controls").parmTemplate().isHidden()
        self.rop.setParms(
            dict(
                vobject=geo.path(),
                camera=camera.path(),
                resx=64,
                resy=48,
                samples=4,
                use_denoise=0,
                use_material_assignments=0,
                material_kind="emission",
                emission_strength=1,
                trange=0,
            )
        )
        self.rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        assert self.rop.evalParm("soho_program") == "CHRender.py"
        assert self.rop.evalParm("soho_outputmode") == 1
        assert f"cycles-{os.getpid()}-{self.rop.sessionId()}-" in self.rop.evalParm("soho_diskfile")
        assert Path(hou.findFile("soho/CHRender.py")).resolve() == self.host / "soho/CHRender.py"
        assert Path(self.rop.type().definition().libraryFilePath()).resolve().is_relative_to(self.host)
        # Existing saved ROPs carry the former unquoted internal channel even
        # after the definition's default changes. OnLoaded must repair it.
        rop_path = self.rop.path()
        self.rop.parm("soho_program").set("$CYCLES_HOUDINI_HOST_ROOT/soho/CHRender.py")
        self.rop.parm("soho_outputmode").set(2)
        scene = self.path.parent / "Existing ROP With Old Program.hiplc"
        hou.hipFile.save(str(scene), save_to_recent_files=False)
        hou.hipFile.load(str(scene), suppress_save_prompt=True, ignore_load_warnings=True)
        self.rop = hou.node(rop_path)
        assert self.rop.evalParm("soho_program") == "CHRender.py"
        assert self.rop.evalParm("soho_outputmode") == 1
        assert self.rop.parm("soho_diskfile") is not None
        assert f"cycles-{os.getpid()}-{self.rop.sessionId()}-" in self.rop.evalParm("soho_diskfile")
        self.report["reopened_rop_program"] = self.rop.evalParm("soho_program")
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(1100, 900))
        self.settings.setCurrentNode(self.rop)
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

    def check_image(self, path):
        assert path.is_file(), (path, self.rop.errors())
        image = oiio.ImageBuf(str(path))
        pixels = image.get_pixels()
        assert pixels is not None, image.geterror()
        channels = list(image.spec().channelnames)
        pixel = pixels[24, 32, [channels.index(c) for c in ("R", "G", "B")]]
        np.testing.assert_allclose(pixel, (0.2, 0.4, 0.8), atol=0.002, rtol=0)
        return pixel.tolist()

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 100, self.phase
            if self.report.get("phase") != self.phase:
                self.report["phase"] = self.phase
                self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            if self.phase == "foreground":
                self.foreground = self.path.parent / "Foreground Render.exr"
                self.rop.parm("picture").set(str(self.foreground))
                self.rop.parm("execute").pressButton()
                assert not self.rop.errors(), self.rop.errors()
                pixel = self.check_image(self.foreground)
                summary = self.rop.cachedUserData(final_render._summary_key)
                assert summary["completed_frames"] == 1, summary
                log = Path(self.rop.evalParm("soho_diskfile"))
                assert log.is_file(), log
                text = log.read_text(encoding="utf-8")
                if "Indie" in str(hou.licenseCategory()):
                    assert "limited_commercial_ifd" in text, text
                self.report["foreground_log"] = dict(
                    path=str(log), license_notice_preserved="limited_commercial_ifd" in text
                )
                self.report["events"].append(dict(button="Render to Disk", pixel=pixel, summary=summary))
                self.background = self.path.parent / "Background Render.exr"
                self.rop.parm("picture").set(str(self.background))
                self.menu_action("render_background")
                self.job = bg.last_job(self.rop)
                assert self.job is not None
                self.phase = "background"
                return
            if self.phase == "background":
                status = self.job.status()
                assert status["state"] not in ("failed", "cancelled"), status
                if status["state"] != "completed":
                    return
                if self.job.poll() is None:
                    return
                assert status["completed_frames"] == 1 and status["pid"] != os.getpid(), status
                pixel = self.check_image(self.background)
                self.report["events"].append(dict(button="Render to Disk in Background", pixel=pixel, status=status))
                self.menu_action("background_jobs")
                manager = background_panel._window
                assert manager.isVisible() and manager.table.rowCount() > 0
                assert not manager.cancel_button.isEnabled()
                manager.close()
                self.report["events"].append(dict(button="Background Renders", job=self.job.directory.name))
                self.before = list(QtWidgets.QApplication.topLevelWidgets())
                self.menu_action("render_settings")
                self.phase = "controls"
                return
            if self.phase == "controls":
                opened = [w for w in QtWidgets.QApplication.topLevelWidgets() if w.isVisible() and w not in self.before]
                if not opened:
                    return
                self.report["controls_windows"] = [
                    dict(title=w.windowTitle(), type=w.metaObject().className()) for w in opened
                ]
                for window in opened:
                    window.close()
                self.report["events"].append(dict(button="Controls", windows=self.report["controls_windows"]))
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def menu_action(self, name):
        self.rop.parm("ch_controls").pressButton()
        menu = rop_controls._controls_menu
        assert menu.isVisible()
        actions = [a for a in menu.actions() if not a.isSeparator()]
        assert [a.data() for a in actions] == [
            "render_settings",
            "render_background",
            "background_jobs",
            "cancel_background",
        ]
        assert all(not a.icon().isNull() for a in actions)
        menu.close()
        next(a for a in actions if a.data() == name).trigger()

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        self.settings.close()
        self.timer.deleteLater()
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
