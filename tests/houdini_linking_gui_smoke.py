# SPDX-License-Identifier: Apache-2.0
"""One artist viewer: native linking edits, bundle/category repair and camera navigation with heavy geometry resident."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry, object_properties, light_properties
from light_shader_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_LINK_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.add_heavy_packed()
        object_properties.install(self.f.obj)
        self.f.camera.parmTuple("res").set((64, 64))
        self.f.light.parmTuple("cycles_light_color").set((1, 0, 0))
        self.green = self.f.scene.createNode(light_properties.LIGHT_TYPE, "green_fill")
        self.green.setParms(
            dict(cycles_light_type="point", tz=3, light_intensity=100, light_exposure=0, cycles_light_radius=0)
        )
        self.green.parmTuple("cycles_light_color").set((0, 1, 0))
        self.f.light.parm("categories").set("red")
        self.green.parm("categories").set("green")
        self.bundle = hou.addNodeBundle("gui_link_bundle")
        self.bundle.addNode(self.f.light)
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            physical_mouse_certification=False,
            scope=__doc__,
            events=[],
        )
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 500))
        self.settings.setCurrentNode(self.f.obj)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
            self.tab.startRender()
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.expected = (1, 1, 0)
        self.camera_x = 0
        self.began = time.monotonic()
        self.next_at = self.saved_at = 0
        self.generation = 0
        self.finished = self.busy = False
        self.commit_deadline = None
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def edit(self, parm, text, phase, expected):
        self.parm = parm
        self.text = str(text)
        self.after = phase
        self.expected = expected
        self.settings.setCurrentNode(parm.node())
        self.settings.setIsCurrentTab()
        for folder, index in zip(parm.containingFolderSetParmTuples(), parm.containingFolderIndices()):
            folder[0].set(1 if folder.parmTemplate().folderType() == hou.folderType.Collapsible else index)
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
            assert now - self.began < 180, self.phase
            if now - self.saved_at > 0.5:
                self.report["phase"] = self.phase
                self.path.write_text(json.dumps(self.report), encoding="utf-8")
                self.saved_at = now
            if now < self.next_at:
                return
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "first"
                return
            if self.phase == "focus":
                assert self.parm.tuple() in self.settings.visibleParms()
                self.settings.scrollTo(self.parm, hou.scrollPosition.Center)
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "keys"
                self.next_at = now + 0.15
                return
            if self.phase == "keys":
                for _ in range(self.parm.componentIndex()):
                    QtTest.QTest.keyClick(QtWidgets.QApplication.focusWidget() or self.window, QtCore.Qt.Key_Tab)
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.text)
                self.phase = "commit"
                self.next_at = now + 0.15
                return
            if self.phase == "commit":
                QtTest.QTest.keyClick(QtWidgets.QApplication.focusWidget() or self.window, QtCore.Qt.Key_Return)
                self.commit_deadline = now + 8
                self.phase = self.after
                self.next_at = now + 0.15
                return
            if self.commit_deadline:
                value = self.parm.eval()
                matches = value == self.text if isinstance(value, str) else float(value) == float(self.text)
                assert matches or now < self.commit_deadline, (self.parm.path(), value, self.text)
                if not matches:
                    return
                self.commit_deadline = None
            entry = registry.get(f"rop:{self.f.rop.sessionId()}")
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert status["state"] != "failed" and not entry.error, (status, entry.error)
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] <= self.generation
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (64, 64):
                    return
                try:
                    shown = np.asarray(self.tab.pixel("C", 32, 32)[:3])
                except hou.OperationFailed:
                    return
                if not np.allclose(shown, self.f.expected(self.expected, camera_x=self.camera_x), atol=0.004, rtol=0):
                    return
            elif self.panel._live_generation != info["generation"]:
                return
            frame = entry.session.frame()
            self.f.check(frame, self.expected, camera_x=self.camera_x)
            assert (
                len(registry.entries()) == 1 and status["packed_instances"] == 12 and status["geometry_resources"] == 2
            ), status
            if self.phase == "first":
                self.pid = status["renderer_pid"]
                self.conversions = status["geometry_conversions"]
                self.record(
                    "One viewer presents both colors with the million-triangle prototype shared by twelve instances",
                    status,
                )
                self.edit(self.f.obj.parm("lightmask"), self.f.light.path(), "red", (1, 0, 0))
                return
            assert status["renderer_pid"] == self.pid and status["geometry_conversions"] == self.conversions, status
            codes = {d["code"] for d in status["source_diagnostics"]}
            assert codes == ({"linking.source_unavailable"} if self.phase == "broken_category" else set()), status
            if self.phase == "red":
                self.record("Native Light Mask keyboard edit selects red", status)
                self.edit(self.f.obj.parm("lightmask"), "* ^" + self.f.light.path(), "green", (0, 1, 0))
                return
            if self.phase == "green":
                self.record("Native exclusion keyboard edit selects green", status)
                self.edit(self.f.obj.parm("lightmask"), "@" + self.bundle.name(), "bundle", (1, 0, 0))
                return
            if self.phase == "bundle":
                self.record("Native bundle reference selects red", status)
                self.bundle.removeNode(self.f.light)
                self.bundle.addNode(self.green)
                self.expected = (0, 1, 0)
                self.phase = "bundle_edit"
                return
            if self.phase == "bundle_edit":
                self.record("Bundle membership edit automatically updates the displayed render", status)
                self.edit(self.f.obj.parm("lightmask"), "*", "all", (1, 1, 0))
                return
            if self.phase == "all":
                self.record("Restoring the mask restores both lights", status)
                self.edit(self.f.obj.parm("lightcategories"), "(red)", "broken_category", (1, 1, 0))
                return
            if self.phase == "broken_category":
                self.record("Broken category keeps rendering with a visible warning", status)
                self.edit(self.f.obj.parm("lightcategories"), "red", "category_repaired", (1, 0, 0))
                return
            if self.phase == "category_repaired":
                self.record("Native category repair changes illumination and clears the warning", status)
                self.camera_x = 0.5
                self.edit(self.f.camera.parm("tx"), 0.5, "camera", (1, 0, 0))
                return
            if self.phase == "camera":
                self.record("Camera navigation retains current link bindings and shared geometry", status)
                if self.native:
                    self.tab.killRender()
                else:
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
        entry = registry.get(f"rop:{self.f.rop.sessionId()}")
        if entry:
            self.report["last"] = dict(phase=self.phase, status=entry.session.status(), info=entry.session.frame_info())
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
