# SPDX-License-Identifier: Apache-2.0
"""One viewer: native keyboard/HOM edits, file repair and camera movement with heavy packed geometry resident."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry, menu_actions
from light_shader_fixture import Fixture, ies_text, write_image


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.add_heavy_packed()
        self.f.light.parm("cycles_light_ies").set(str(self.f.profile))
        self.report = dict(result="running", events=[], physical_mouse_certification=False, scope=__doc__)
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 500))
        self.settings.setCurrentNode(self.f.light)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.phase = "mount"
        self.panel = None
        self.busy = self.finished = False
        self.began = time.monotonic()
        self.next_at = 0
        self.saved_at = 0
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def edit(self, parm, text, after):
        self.parm = parm
        self.text = str(text)
        self.after = after
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
        if self.busy or self.finished:
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
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "first"
                return
            if self.phase == "focus":
                assert self.parm.tuple() in self.settings.visibleParms(), self.parm.path()
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
                assert self.text, "Empty native references use explicit HOM revert-to-defaults coverage below"
                QtTest.QTest.keyClicks(focus, self.text)
                self.report.setdefault("keyboard_edits", []).append(
                    dict(
                        path=self.parm.path(),
                        requested=self.text,
                        value=self.parm.eval(),
                        focus_type=type(focus).__name__,
                    )
                )
                self.phase = "commit_keys"
                self.next_at = now + 0.15
                return
            if self.phase == "commit_keys":
                QtTest.QTest.keyClick(QtWidgets.QApplication.focusWidget() or self.window, QtCore.Qt.Key_Return)
                self.commit_deadline = now + 8
                self.phase = self.after
                self.next_at = now + 0.15
                return
            if getattr(self, "commit_deadline", None):
                value = self.parm.eval()
                matches = value == self.text if isinstance(value, str) else float(value) == float(self.text)
                assert matches or now < self.commit_deadline, (self.parm.path(), self.text, value)
                if not matches:
                    return
                self.report["keyboard_edits"][-1]["committed_value"] = value
                self.commit_deadline = None
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert status["state"] != "failed" and not entry.error, (status, entry.error)
            if (
                not info
                or not info["complete"]
                or self.panel._live_generation != info["generation"]
                or info["generation"] != status["generation"]
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if self.phase != "first" and info["generation"] <= self.generation:
                return
            assert len(registry.entries()) == 1 and entry.views == 1
            frame = entry.session.frame()
            issues = status["source_diagnostics"]
            if self.phase == "first":
                self.f.check(frame)
                assert status["packed_instances"] == 12 and status["geometry_resources"] == 2, status
                faces = self.f.heavy.node("million_triangle_prototype").geometry().intrinsicValue("primitivecount")
                assert faces >= 500000, faces
                self.report["prototype_polygons"] = faces
                self.report["instances"] = 12
                self.pid = status["renderer_pid"]
                self.conversions = status["geometry_conversions"]
                self.record("Heavy shared geometry resident; IES matches independent illumination", status)
                self.edit(self.f.light.parm("light_intensity"), 200, "power")
                return
            assert status["renderer_pid"] == self.pid and status["geometry_conversions"] == self.conversions, status
            if self.phase == "power":
                assert self.f.light.evalParm("light_intensity") == 200
                self.f.check(frame, power=200)
                self.record("Native Power keyboard edit changes illumination", status)
                self.f.profile.write_text("broken", encoding="ascii")
                self.phase = "broken"
                return
            if self.phase == "broken":
                if not any(d["code"] == "light.ies_unavailable" for d in issues):
                    return
                self.f.check(frame, power=200)
                self.record("File-only profile break retains light and supported geometry", status)
                self.f.profile.write_text(ies_text(2), encoding="ascii")
                self.phase = "repaired"
                return
            if self.phase == "repaired":
                if issues:
                    return
                self.f.check(frame, 2, power=200)
                self.record("File-only profile repair restores IES and clears warning automatically", status)
                self.edit(self.f.parent.parm("tx"), 1, "parent")
                return
            if self.phase == "parent":
                assert self.f.parent.evalParm("tx") == 1
                self.f.check(frame, 2, power=200, light_x=1)
                self.record("Parent transform keyboard edit updates textured/IES light position", status)
                self.edit(self.f.camera.parm("tx"), 0.5, "camera")
                return
            if self.phase == "camera":
                assert self.f.camera.evalParm("tx") == 0.5
                self.f.check(frame, 2, power=200, light_x=1, camera_x=0.5)
                self.record(
                    "Camera edit presents the expected image while heavy shared geometry stays resident", status
                )
                self.f.light.parm("cycles_light_texture_colorspace").set("data")
                self.edit(self.f.light.parm("cycles_light_texture"), self.f.texture.as_posix(), "texture")
                return
            if self.phase == "texture":
                assert os.path.normpath(self.f.light.evalParm("cycles_light_texture")) == os.path.normpath(
                    str(self.f.texture)
                ), self.f.light.evalParm("cycles_light_texture")
                if not any(
                    os.path.normpath(i["filename"]) == os.path.normpath(str(self.f.texture))
                    for i in entry.session.texture_inputs()
                ):
                    return
                self.f.check(frame, (0.5, 1, 1.5), power=200, light_x=1, camera_x=0.5)
                self.record("Native file parameter keyboard edit applies color texture", status)
                self.f.texture.unlink()
                self.phase = "texture_broken"
                return
            if self.phase == "texture_broken":
                if not any(d["code"] == "light.texture_unavailable" for d in issues):
                    return
                self.f.check(frame, 2, power=200, light_x=1, camera_x=0.5)
                self.record("Missing light color texture keeps valid IES and geometry", status)
                write_image(self.f.texture, (0.5, 0.25, 0.125))
                self.phase = "texture_repaired"
                return
            if self.phase == "texture_repaired":
                if issues:
                    return
                self.f.check(frame, (1, 0.5, 0.25), power=200, light_x=1, camera_x=0.5)
                self.record("File-only light texture repair updates the displayed image", status)
                self.material = menu_actions.create_light_material()
                self.shader = self.material.node("emission")
                self.shader.parmTuple("color").set((0.25, 0.5, 0.75))
                self.shader.parm("strength").set(1)
                self.edit(self.f.light.parm("cycles_light_material"), self.material.path(), "material")
                return
            if self.phase == "material":
                self.f.check(frame, (0.25, 0.5, 0.75), power=200, light_x=1, camera_x=0.5)
                self.record("Native Emission Material assignment overrides dormant shortcuts", status)
                self.edit(self.shader.parm("strength"), 2, "material_edit")
                return
            if self.phase == "material_edit":
                self.f.check(frame, (0.5, 1, 1.5), power=200, light_x=1, camera_x=0.5)
                self.record("Native shader keyboard edit is observed through the assigned light material", status)
                parm = self.f.light.parm("cycles_light_material")
                parm.revertToDefaults()
                assert parm.eval() == ""
                self.report["empty_reference_action"] = dict(
                    method="HOM revertToDefaults",
                    keyboard_clear_certified=False,
                    reason="Synthetic delete keys did not commit an empty native reference; no physical keyboard claim",
                )
                self.phase = "material_removed"
                return
            if self.phase == "material_removed":
                self.f.check(frame, (1, 0.5, 0.25), power=200, light_x=1, camera_x=0.5)
                self.record(
                    "HOM Revert to Defaults clears emission assignment and restores texture/IES shortcuts", status
                )
                cops = hou.node("/img").createNode("img", "live_light_cops")
                self.cop = cops.createNode("color", "color")
                self.cop.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
                self.cop.parmTuple("color").set((0.2, 0.4, 0.6, 1))
                self.edit(self.f.light.parm("cycles_light_texture"), "op:" + self.cop.path(), "cop")
                return
            if self.phase == "cop":
                assert self.f.light.evalParm("cycles_light_texture") == "op:" + self.cop.path()
                if not any(i["filename"] == "op:" + self.cop.path() for i in entry.session.texture_inputs()):
                    return
                self.f.check(frame, (0.4, 0.8, 1.2), power=200, light_x=1, camera_x=0.5)
                self.record("Native op: light image source reaches the persistent worker", status)
                self.cop.parm("colorr").set(0.7)
                self.phase = "cop_edit"
                return
            if self.phase == "cop_edit":
                self.f.check(frame, (1.4, 0.8, 1.2), power=200, light_x=1, camera_x=0.5)
                self.record(
                    "Uncooked upstream COP edits automatically update the light with heavy geometry retained", status
                )
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
        if self.panel and self.panel.entry:
            entry = self.panel.entry
            self.report["last"] = dict(
                phase=self.phase,
                status=entry.session.status(),
                info=entry.session.frame_info(),
                serials=[entry.change_serial, entry.applied_serial, entry.requested_serial],
                texture=self.f.light.evalParm("cycles_light_texture"),
                material=self.f.light.evalParm("cycles_light_material"),
                inputs=entry.session.texture_inputs(),
            )
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
