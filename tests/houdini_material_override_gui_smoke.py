# SPDX-License-Identifier: Apache-2.0
"""One viewer: native Material SOP keyboard edits and file repair with heavy shared geometry."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry
from light_shader_fixture import Fixture, write_image


class Workflow:
    def __init__(self, *, auto_start=True):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_OVERRIDE_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.add_heavy_packed()
        self.f.sop.setParms(dict(rows=513, cols=1025))
        self.f.camera.parmTuple("res").set((64, 64))
        self.f.rop.setParms(dict(use_material_assignments=1, samples=8, ipr_samples=8))
        self.material = ch.create_material(name="gui_override_material", shader="emission")
        surface = self.material.node("surface_shader")
        surface.parm("strength").set(1)
        ch.promote_parameter(self.material, surface, "strength", name="power")
        self.directory = self.f.dest.parent / "material-override-gui"
        self.directory.mkdir(exist_ok=True)
        self.color = self.directory / "color.exr"
        self.missing = self.directory / "repaired.exr"
        if self.missing.exists():
            self.missing.unlink()
        write_image(self.color, (0.2, 0.4, 0.6))
        texture = self.material.createNode("cycles::image_texture::1", "color_image")
        texture.setParms(dict(filename=str(self.color), colorspace="data", interpolation="closest"))
        coordinates = self.material.createNode("cycles::texture_coordinate::1", "coordinates")
        texture.setNamedInput("vector", coordinates, "generated")
        surface.setNamedInput("color", texture, "color")
        ch.promote_parameter(self.material, texture, "filename", name="image_file")
        displacement = self.material.createNode("cycles::vector_displacement::1", "offset")
        # The default this test was written for (new nodes now take Blender's).
        displacement.setParms(dict(scale=1.0, space="object"))
        displacement.parmTuple("vector").set((0, 0, 0.15))
        self.material.node("surface_output").setNamedInput("displacement", displacement, "displacement")
        self.material.parm("cycles_displacement_method").set("true")
        self.sop = self.f.obj.createNode("material", "artist_local_overrides")
        self.sop.setInput(0, self.f.sop)
        self.sop.setParms(
            dict(
                shop_materialpath1=self.material.path(),
                num_local1=2,
                local1_name1="power",
                local1_type1="float",
                local1_fval1=1,
                local1_name2="image_file",
                local1_type2="string",
                local1_sval2=str(self.color),
            )
        )
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            physical_mouse_certification=False,
            scope=__doc__,
            events=[],
            tick_intervals=[],
        )
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 550))
        self.settings.setCurrentNode(self.sop)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
            if auto_start:
                self.tab.startRender()
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.expected = (0.2, 0.4, 0.6)
        self.began = self.last_tick = time.monotonic()
        self.next_at = self.saved_at = 0
        self.generation = 0
        self.finished = self.busy = False
        self.commit_deadline = None
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        # Native panes can service Houdini callbacks without pumping a plain
        # Qt timer. Drive their assertions through the host's own UI loop.
        if self.native:
            hou.ui.addEventLoopCallback(self.tick)
        else:
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
            self.report["tick_intervals"].append(dict(phase=self.phase, seconds=now - self.last_tick))
            self.last_tick = now
            assert now - self.began < 200, self.phase
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
                if not np.allclose(shown, self.expected, atol=2e-5, rtol=0):
                    return
            elif self.panel._live_generation != info["generation"]:
                return
            frame = entry.session.frame()
            np.testing.assert_allclose(
                self.f.pixels(frame)[31:33, 31:33, :3], np.broadcast_to(self.expected, (2, 2, 3)), atol=2e-5, rtol=0
            )
            assert (
                len(registry.entries()) == 1 and status["packed_instances"] == 12 and status["geometry_resources"] == 2
            ), status
            if self.phase == "first":
                self.pid = status["renderer_pid"]
                self.conversions = status["geometry_conversions"]
                self.rebuilds = status["displacement_rebuilds"]
                self.record(
                    "One viewer renders a million-triangle edited mesh alongside twelve heavy shared instances", status
                )
                self.edit(self.sop.parm("local1_fval1"), 2, "power", (0.4, 0.8, 1.2))
                return
            assert (
                status["renderer_pid"] == self.pid
                and status["geometry_conversions"] == self.conversions
                and status["displacement_rebuilds"] == self.rebuilds
            ), status
            codes = {d["code"] for d in status["source_diagnostics"]}
            assert codes == (
                {"material.override_ignored"}
                if self.phase == "broken"
                else {"texture.unavailable"}
                if self.phase == "file_broken"
                else set()
            ), status
            if self.phase == "power":
                self.record(
                    "Native Local Override keyboard edit changes emission without geometry or displacement rebuild",
                    status,
                )
                self.edit(self.sop.parm("local1_name1"), "unknown", "broken", (0.2, 0.4, 0.6))
                return
            if self.phase == "broken":
                self.record("Invalid override retains authored shading and a useful warning", status)
                self.edit(self.sop.parm("local1_name1"), "power", "repaired", (0.4, 0.8, 1.2))
                return
            if self.phase == "repaired":
                self.record("Keyboard repair restores the override and clears its warning", status)
                self.edit(self.sop.parm("local1_sval2"), str(self.missing), "file_broken", (2, 0, 2))
                return
            if self.phase == "file_broken":
                self.record("Missing overridden color image retains independent displacement", status)
                write_image(self.missing, (0.1, 0.3, 0.5))
                self.expected = (0.2, 0.6, 1)
                self.phase = "file_repaired"
                return
            if self.phase == "file_repaired":
                self.record("Writing the missing file automatically repairs the displayed image", status)
                self.edit(self.f.camera.parm("tx"), 0.5, "camera", (0.2, 0.6, 1))
                return
            if self.phase == "camera":
                self.record("Camera parameter navigation retains both heavy geometry resources", status)
                capture = self.path.parent / "material-overrides-viewer.png"
                window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
                self.report["viewer_capture"] = str(capture)
                self.report["viewer_capture_saved"] = window.grab().save(str(capture))
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
        if self.native and self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
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


if __name__ == "__main__":
    workflow = Workflow()
