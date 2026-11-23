# SPDX-License-Identifier: Apache-2.0
"""Native world/volume shelf previews, real parameter keyboard edits and warning action dispatch."""

import faulthandler, json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import _native, material_previews as mp, registry
from shader_bypass_preview_fixture import Fixture

stack_log = open(str(Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).with_suffix(".python-stacks.log")), "w")
faulthandler.dump_traceback_later(25, repeat=True, file=stack_log)


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(
            result="running", scope=__doc__, events=[], physical_mouse_certification=False, nonmodal_warning_window=True
        )
        self.report["phase"] = "construct"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        if mp._service:
            mp._service.close(wait=True)
        mp._service = self.service = mp.PreviewService(samples=8, idle_seconds=30)
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.rop.setParms(dict(ipr_samples=1048576, ipr_adaptive_sampling=0))
        self.report["phase"] = "world"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        self.world = ch.create_world_material(name="world_preview_ui")
        self.world.node("background_shader").parmTuple("color").set((0.15, 0.3, 0.6))
        self.report["phase"] = "volume"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        self.volume = ch.create_material(name="volume_preview_ui", shader="absorption_volume")
        self.volume.node("volume_shader").parmTuple("color").set((0.2, 0.6, 0.3))
        self.attribute = self.volume.createNode("cycles::attribute::1", "editable_density_field")
        self.attribute.parm("attribute").set("density")
        self.volume.node("density_scale").setNamedInput("value1", self.attribute, "fac")
        self.report["phase"] = "panes"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.network = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(650, 500))
        self.network.setPwd(hou.node("/mat"))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.report["phase"] = "interface"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.phase = "mount"
        self.finished = self.busy = False
        self.next_at = 0
        self.began = time.monotonic()
        self.last_report_at = 0
        self.reported_phase = None
        self.report["phase"] = self.phase
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def request(self, node):
        node.setSelected(True, clear_all_selected=True)
        tool = hou.shelves.tools()["cycles_material_preview"]
        exec(compile(tool.script(), "<Material Preview shelf>", "exec"), {})
        assert self.network.getPref("showpreviews") == "1"

    def image(self, node):
        return _native.thumbnail_pixels(node.sessionId())

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

    def warnings(self, expected):
        action = next(
            a for a in self.panel.diagnostics_menu.menu().actions() if a.text() == "Material Preview Warnings"
        )
        action.trigger()
        dialog = mp._warnings_dialog
        assert dialog is not None and dialog.isVisible() and not dialog.isModal()
        captured = dict(message=dialog.summary.text(), details=dialog.details.toPlainText())
        if expected:
            assert self.volume.path() in captured["details"] and "missing_density" in captured["details"], captured
            assert "1 warning" in captured["message"], captured
        else:
            assert "0 warning" in captured["message"], captured
        self.warning_dialog = dialog
        return captured

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            assert now - self.began < 150, (self.phase, self.service.diagnostics())
            if self.reported_phase != self.phase or now - self.last_report_at > 0.5:
                self.report["phase"] = self.phase
                self.report["preview_progress"] = self.service.diagnostics()
                self.path.write_text(json.dumps(self.report), encoding="utf-8")
                self.last_report_at = now
                self.reported_phase = self.phase
            if now < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                self.request(self.world)
                self.phase = "world"
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
                self.phase = self.after
                self.next_at = now + 0.15
                return
            if self.phase == "world":
                if not self.image(self.world):
                    return
                self.before = self.image(self.world)
                self.report["events"].append("Registered shelf action renders native World material raster")
                self.edit(self.world.node("background_shader").parm("strength"), 2, "world-edited")
                return
            if self.phase == "world-edited":
                assert self.world.node("background_shader").evalParm("strength") == 2
                if self.image(self.world) == self.before:
                    return
                self.report["events"].append("Native parameter keyboard edit changes world preview")
                self.request(self.volume)
                self.phase = "volume"
                return
            if self.phase == "volume":
                if not self.image(self.volume):
                    return
                self.before = self.image(self.volume)
                self.report["events"].append("Same native shelf previews a volume without scene objects")
                self.edit(self.attribute.parm("attribute"), "missing_density", "broken")
                return
            if self.phase == "broken":
                assert self.attribute.evalParm("attribute") == "missing_density"
                if self.volume.sessionId() not in self.service.errors or self.image(self.volume) == self.before:
                    return
                self.report["events"].append(
                    dict(action="Preview warning menu identifies source and omitted field", dialog=self.warnings(True))
                )
                self.edit(self.attribute.parm("attribute"), "density", "repaired")
                return
            if self.phase == "repaired":
                assert self.attribute.evalParm("attribute") == "density"
                if self.volume.sessionId() in self.service.errors or self.service.active:
                    return
                if "0 warning" not in self.warning_dialog.summary.text():
                    return
                assert self.warning_dialog.isVisible() and not self.warning_dialog.isModal()
                self.report["events"].append(
                    dict(
                        action="Editing remains possible while warning window is open; repair updates its details automatically",
                        dialog=dict(
                            message=self.warning_dialog.summary.text(),
                            details=self.warning_dialog.details.toPlainText(),
                        ),
                    )
                )
                self.warning_dialog.close()
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "ipr"
                return
            if self.phase == "ipr":
                entry = self.panel.entry
                if entry is None:
                    return
                status = entry.session.status()
                info = entry.session.frame_info()
                if not info or self.panel._live_generation != info["generation"]:
                    return
                assert status["state"] == "rendering" and not entry.error, (status, entry.error)
                self.completed = self.service.completed
                self.geometry = status["geometry_conversions"]
                self.generation = info["generation"]
                self.world_token = self.service.request(self.world, 64)
                self.volume_token = self.service.request(self.volume, 64)
                self.report["events"].append("One artist IPR retains priority over pending world and volume previews")
                self.edit(self.f.camera.parm("tx"), 0.25, "camera-active")
                return
            if self.phase == "camera-active":
                assert abs(self.f.camera.evalParm("tx") - 0.25) < 1e-6
                entry = self.panel.entry
                status = entry.session.status()
                info = entry.session.frame_info()
                if (
                    not info
                    or info["generation"] <= self.generation
                    or self.panel._live_generation != info["generation"]
                ):
                    return
                assert status["geometry_conversions"] == self.geometry and not entry.error, status
                assert self.service.completed == self.completed and self.service.paused, self.service.diagnostics()
                assert len(registry.entries()) == 1 and entry.views == 1
                self.report["events"].append(
                    dict(
                        action="Native camera keyboard edit updates one IPR while both preview types remain paused",
                        status=status,
                    )
                )
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.phase = "resume"
                return
            if self.phase == "resume":
                if self.service.completed < self.completed + 2 or self.service.active or self.service.pending:
                    return
                assert not self.service.paused
                for token in (self.world_token, self.volume_token):
                    assert any(
                        e["event"] == "published" and e["token"][0] == token[0] and e["token"][1] >= token[1]
                        for e in self.service.events
                    ), self.service.diagnostics()
                self.report["events"].append("Stopped IPR releases both pending preview types")
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
        faulthandler.cancel_dump_traceback_later()
        stack_log.close()
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.settings.close()
        self.network.close()
        self.tab.close()
        self.timer.deleteLater()
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
