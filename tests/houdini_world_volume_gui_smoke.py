# SPDX-License-Identifier: Apache-2.0
"""One native Cycles Viewer: world density, field repair and camera-distance edits."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry
from world_volume_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.report = dict(result="running", events=[], physical_mouse_certification=False)
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setCurrentNode(self.f.medium)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.panel = None
        self.finished = self.busy = False
        self.next_at = 0
        self.began = time.monotonic()
        self.last_save = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def edit(self, parm, text, after):
        self.parm, self.text, self.after = parm, str(text), after
        self.settings.setCurrentNode(parm.node())
        self.settings.setIsCurrentTab()
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus()
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.15

    def record(self, label, status):
        self.report["events"].append(dict(label=label, status=status))
        self.generation = status["generation"]

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            assert now - self.began < 150, self.phase
            if now - self.last_save > 0.5:
                self.report["phase"] = self.phase
                self.path.write_text(json.dumps(self.report), encoding="utf-8")
                self.last_save = now
            if now < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "first"
                return
            if self.phase == "focus":
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "keys"
                self.next_at = now + 0.15
                return
            if self.phase == "keys":
                for _ in range(self.parm.componentIndex()):
                    QtTest.QTest.keyClick(QtWidgets.QApplication.focusWidget() or self.window, QtCore.Qt.Key_Tab)
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                if self.text:
                    QtTest.QTest.keyClicks(focus, self.text)
                else:
                    QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Backspace)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = self.after
                self.next_at = now + 0.15
                return
            if self.phase == "camera":
                assert abs(self.f.camera.evalParm("tz") - 4) < 1e-6, self.f.camera.parmTuple("t").eval()
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert status["state"] != "failed" and not entry.error, (status, entry.error)
            if not info or not info["complete"] or self.panel._live_generation != info["generation"]:
                return
            if now - self.last_save < 0.05:
                self.report["progress"] = dict(
                    status=status,
                    info=info,
                    change_serial=entry.change_serial,
                    applied_serial=entry.applied_serial,
                    camera=self.f.camera.parmTuple("t").eval(),
                    live_generation=self.panel._live_generation,
                )
            if status.get("capture_pending") or status.get("pending_update"):
                return
            if self.phase != "first" and info["generation"] <= self.generation:
                return
            assert len(registry.entries()) == 1 and entry.views == 1
            frame = entry.session.frame()
            if self.phase == "first":
                self.f.check(frame, 0.4)
                self.geometry = status["geometry_conversions"]
                self.pid = status["renderer_pid"]
                self.record("World fog matches independent attenuation in one viewer", status)
                self.edit(self.f.medium.parm("density"), 0.2, "density")
                return
            assert status["geometry_conversions"] == self.geometry and status["renderer_pid"] == self.pid, status
            if self.phase == "density":
                assert abs(self.f.medium.evalParm("density") - 0.2) < 1e-6
                self.f.check(frame, 0.2)
                self.record("Native parameter keyboard edit updates world density without geometry reload", status)
                self.principled = self.f.world.createNode("cycles::principled_volume::1", "repairable_world")
                self.principled.setParms(dict(density=0.2, color_attribute="missing_grid"))
                self.principled.parmTuple("color").set((0, 0, 0))
                self.f.output.setNamedInput("volume", self.principled, "volume")
                self.phase = "broken"
                return
            if self.phase == "broken":
                if not any(d["code"] == "world.volume_source_unavailable" for d in status["source_diagnostics"]):
                    return
                self.f.check(frame, 0, background=(0.1, 0.2, 0.4))
                assert any(d["code"] == "world.volume_source_unavailable" for d in status["source_diagnostics"]), status
                self.record("Bad world-grid input retains geometry and independent background", status)
                self.edit(self.principled.parm("color_attribute"), "", "repaired")
                return
            if self.phase == "repaired":
                assert self.principled.evalParm("color_attribute") == ""
                if status["source_diagnostics"]:
                    return
                self.f.check(frame, 0.2)
                assert not status["source_diagnostics"], status
                self.record("Live parameter repair restores fog and clears warnings", status)
                self.edit(self.f.camera.parm("tz"), 4, "camera")
                return
            if self.phase == "camera":
                assert abs(self.f.camera.evalParm("tz") - 4) < 1e-6
                self.f.check(frame, 0.2, distance=4)
                self.record("Camera edit changes optical path length without geometry conversion", status)
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
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
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.settings.close()
        self.tab.close()
        self.timer.deleteLater()
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
