# SPDX-License-Identifier: Apache-2.0
"""Native shelf/node thumbnails and parameter keyboard edits; one IPR keeps priority."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry, _native
from cycles_houdini import material_previews as mp
from shader_bypass_preview_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(
            result="running", scope=__doc__, events=[], heartbeat_gaps=[], physical_mouse_certification=False
        )
        if mp._service:
            mp._service.close(wait=True)
        mp._service = self.service = mp.PreviewService(samples=8)
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.output.setNamedInput("surface", f.emission, "emission")
        f.emission.parmTuple("color").set((0.2, 0.4, 0.8))
        f.rop.setParms(dict(ipr_samples=1048576, ipr_adaptive_sampling=0))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setCurrentNode(f.emission)
        self.network = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(650, 500))
        self.network.setPwd(f.builder.parent())
        self.network.setPref("showpreviews", "1")
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.panel = None
        self.busy = self.finished = False
        self.began = self.beat = time.monotonic()
        self.next_at = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def edit(self, parm, text, phase):
        self.parm, self.text, self.after_keys = parm, str(text), phase
        self.settings.setCurrentNode(parm.node())
        self.settings.setIsCurrentTab()
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus()
        QtGui.QCursor.setPos(self.window.mapToGlobal(self.window.rect().center()))
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.15

    def swatch(self):
        return _native.thumbnail_pixels(self.f.builder.sessionId())

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["heartbeat_gaps"].append(now - self.beat)
            self.beat = now
            assert now - self.began < 150, (self.phase, self.service.diagnostics())
            if now < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                self.f.builder.setSelected(True, clear_all_selected=True)
                tool = hou.shelves.tools()["cycles_material_preview"]
                assert tool.icon() and not hou.qt.Icon(tool.icon()).isNull()
                assert not hou.qt.Icon(self.f.builder.type().icon()).isNull()
                exec(compile(tool.script(), "<Cycles Material Preview shelf>", "exec"), {})
                assert self.network.getPref("showpreviews") == "1"
                self.phase = "first"
                return
            if self.phase == "focus":
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "keys"
                self.next_at = now + 0.15
                return
            if self.phase == "keys":
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.text)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = self.after_keys
                self.next_at = now + 0.15
                return
            if self.phase == "first":
                if self.service.completed < 1 or self.service.active or not self.swatch():
                    return
                self.before = self.swatch()
                self.previous = self.service.completed
                self.report["events"].append(
                    dict(
                        action="Registered shelf action populates the native host thumbnail",
                        stats=self.service.diagnostics(),
                    )
                )
                self.edit(self.f.emission.parm("strength"), 2, "shader-edited")
                return
            if self.phase == "shader-edited":
                assert abs(self.f.emission.evalParm("strength") - 2) < 1e-6
                if self.service.completed <= self.previous or self.service.active or not self.swatch():
                    return
                assert self.before != self.swatch()
                self.report["events"].append(
                    dict(action="Native parameter keyboard edit updates the swatch", stats=self.service.diagnostics())
                )
                self.previous = self.service.completed
                self.service.request(self.f.builder)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "ipr"
                return
            if self.phase == "ipr":
                entry = self.panel.entry
                if entry is None:
                    return
                status = entry.session.status()
                info = entry.session.frame_info()
                assert status["state"] != "failed" and not entry.error, (status, entry.error)
                if not info or status.get("current_sample", 0) < 1 or not self.service.paused:
                    return
                assert self.service.completed == self.previous and self.service.pending, self.service.diagnostics()
                self.geometry = status["geometry_conversions"]
                self.generation = info["generation"]
                self.report["events"].append(
                    dict(
                        action="One IPR renders while preview requests remain bounded and paused",
                        status=status,
                        stats=self.service.diagnostics(),
                    )
                )
                self.edit(self.f.camera.parm("tx"), 0.25, "camera-edited")
                return
            if self.phase == "camera-edited":
                assert abs(self.f.camera.evalParm("tx") - 0.25) < 1e-6
                entry = self.panel.entry
                status = entry.session.status()
                info = entry.session.frame_info()
                assert status["state"] != "failed" and not entry.error, (status, entry.error)
                if (
                    not info
                    or info["generation"] <= self.generation
                    or self.panel._live_generation != info["generation"]
                ):
                    return
                assert status["geometry_conversions"] == self.geometry, status
                assert self.service.completed == self.previous and self.service.paused
                assert len(registry.entries()) == 1 and entry.views == 1
                self.report["events"].append(
                    dict(
                        action="Camera keyboard edit updates IPR without geometry conversion or preview interference",
                        status=status,
                    )
                )
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.phase = "resume"
                return
            if self.phase == "resume":
                if self.service.completed <= self.previous or self.service.active:
                    return
                assert self.swatch() and not self.service.paused
                self.report["events"].append(
                    dict(action="Stopped IPR releases preview work", stats=self.service.diagnostics())
                )
                gaps = sorted(self.report["heartbeat_gaps"][2:])
                self.report.update(
                    heartbeat_p95_seconds=gaps[int((len(gaps) - 1) * 0.95)], heartbeat_max_seconds=max(gaps)
                )
                assert max(gaps) < 1, self.report
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
        self.report.update(result="failed" if error else "passed", error=error, final=self.service.diagnostics())
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        self.service.close(wait=True)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.settings.close()
        self.network.close()
        self.tab.close()
        self.timer.deleteLater()
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
