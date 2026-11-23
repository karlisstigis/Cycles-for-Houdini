# SPDX-License-Identifier: Apache-2.0
from cycles_houdini.device_selection import set_devices as set_rop_devices
import hou, os, time, json, traceback
from pathlib import Path
from PySide6 import QtCore, QtTest
from cycles_houdini.panel import RenderPanel
from cycles_houdini import registry, device_selection, menu_actions


class Test:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        # The recovery under test is an empty GPU selection ("No GPU selected"):
        # with the CPU device the GPU toggles are inactive and nothing fails.
        if os.environ["CYCLES_HOUDINI_GUI_DEVICE"] == "CPU":
            raise RuntimeError("houdini_viewer_start_smoke.py needs a GPU device (-Device <CUDA or OptiX id>), not CPU")
        self.start = time.monotonic()
        self.clicked = False
        self.stage = 0
        obj = hou.node("/obj").createNode("geo", "viewer_start", run_init_scripts=False)
        box = obj.createNode("box")
        camera = hou.node("/obj").createNode("cam", "viewer_camera")
        camera.parm("tz").set(5)
        self.rop = menu_actions.create_rop()
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                source_mode="sop",
                soppath=box.path(),
                camera=camera.path(),
                world_source="color",
                resx=320,
                resy=180,
                ipr_resolution_percent=100,
                ipr_samples=8,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
            )
        )
        self.rop.parmTuple("background").set((0.2, 0.2, 0.2))
        self.saved = {p.name(): self.rop.evalParm(p.name()) for p in device_selection.gpu_parameters(self.rop)}
        assert any(self.saved.values()), "Saved scene lost its device selection"
        self.rop.setParms({name: 0 for name in self.saved})
        self.recovered = False
        self.panel = RenderPanel()
        self.panel.resize(1000, 700)
        self.panel.show()
        self.panel.set_rop(self.rop)
        self.panel.auto.setChecked(False)
        hou.ui.addEventLoopCallback(self.tick)

    def tick(self):
        try:
            if not self.clicked:
                self.clicked = True
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
            entry = self.panel.entry
            status = entry.session.status() if entry else {}
            report = dict(
                result="running",
                info=self.panel.info.text(),
                queued=self.panel._render_queued,
                in_render=self.panel._in_render,
                enabled=self.panel.render_button.isEnabled(),
                status=status,
            )
            self.path.write_text(json.dumps(report, indent=2))
            if not self.recovered and not self.panel._render_queued and "No GPU selected" in self.panel.info.text():
                assert self.panel.render_button.isEnabled(), "Invalid settings stranded the Render button"
                self.recovered = True
                self.rop.setParms(self.saved)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                return
            if entry and entry.error:
                raise RuntimeError(entry.error)
            frame = entry.session.frame_info() if entry else None
            if self.stage in (1, 3) and not self.panel._stopped:
                assert not self.panel.render_button.isEnabled(), "Render re-enabled during an edit"
                assert self.panel.stop_button.isEnabled(), "Stop disabled during an edit"
            if self.stage == 2 and time.monotonic() - self.since > 0.5:
                assert entry.session.status()["generation"] == self.generation, (
                    "Paused viewport edit restarted rendering"
                )
                assert self.panel.pause_button.isChecked()
                QtTest.QTest.mouseClick(self.panel.pause_button, QtCore.Qt.LeftButton)
                self.stage = 3
            if self.stage == 4 and time.monotonic() - self.since > 0.5:
                assert self.panel.render_button.isEnabled()
                assert not self.panel.pause_button.isEnabled() and not self.panel.stop_button.isEnabled()
                assert entry.session.status()["generation"] == self.generation, (
                    "Stopped viewport edit restarted rendering"
                )
                report.update(
                    result="passed",
                    recovered_from_invalid_device=True,
                    mode_checks="completion, camera edit, pause, resume, stop",
                )
                self.finish(report)
                return
            if (
                frame
                and frame["complete"]
                and self.panel._live_generation == frame["generation"]
                and self.stage in (0, 1, 3)
            ):
                assert self.recovered
                if self.stage and frame["generation"] <= self.generation:
                    return
                self.panel.update_render_controls()
                assert not self.panel.render_button.isEnabled(), "Convergence ended render mode"
                assert self.panel.pause_button.isEnabled() and self.panel.stop_button.isEnabled()
                self.generation = frame["generation"]
                camera = hou.node(self.rop.evalParm("camera"))
                if self.stage == 0:
                    self.panel.auto.setChecked(True)
                    camera.parm("tx").set(camera.evalParm("tx") + 0.1)
                    self.stage = 1
                elif self.stage == 1:
                    QtTest.QTest.mouseClick(self.panel.pause_button, QtCore.Qt.LeftButton)
                    camera.parm("tx").set(camera.evalParm("tx") + 0.1)
                    self.stage = 2
                    self.since = time.monotonic()
                else:
                    QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                    camera.parm("tx").set(camera.evalParm("tx") + 0.1)
                    self.stage = 4
                    self.since = time.monotonic()
            if time.monotonic() - self.start > 75:
                raise TimeoutError(str(report))
        except Exception:
            self.finish(dict(result="failed", error=traceback.format_exc()))

    def finish(self, report):
        self.path.write_text(json.dumps(report, indent=2))
        hou.ui.removeEventLoopCallback(self.tick)
        self.panel.dispose()
        self.panel.close()
        registry.close_all()
        hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True)


test = Test()
