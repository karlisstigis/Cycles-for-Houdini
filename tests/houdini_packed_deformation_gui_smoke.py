# SPDX-License-Identifier: Apache-2.0
"""Heavy shared deformation, native parameter edits and one live viewer."""

import json, os, time, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry
from packed_deformation_fixture import Fixture


class Workflow:
    fixture_type = Fixture
    camera_animation = "rint($FF)-1+.12"
    changed_stages = (1, 2, 4, 8, 10, 11, 12, 13)
    retained_stages = (3, 5, 6, 7, 9)
    scope = "One native viewer and Scene Viewer; 1,036,800-triangle shared prototype, 16 nested instances, actual native parameter keyboard edits, accepted canvas revisions and independent coverage. Five animation frames. Physical mouse dragging/display and latency certification are separate."

    def motion_controls(self, f):
        return f.motion.parm("test_speed"), f.motion.parm("test_broken"), 1

    def render_source(self, f):
        return f.nested

    def animated_position(self):
        return hou.frame() - 1 if self.stage >= 9 else 0

    def check_status(self, status):
        pass

    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = self.fixture_type(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.f
        self.scale_parm, self.broken_parm, self.broken_value = self.motion_controls(f)
        f.motion.parm("test_rows").set(721)
        f.targets.setParms(dict(rows=4, cols=4))
        f.targets.parmTuple("size").set((6, 6))
        self.rop = f.rop(self.render_source(f), interactive_samples=256)
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 500))
        self.settings.setCurrentNode(f.motion)
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(700, 500))
        self.scene.curViewport().setCamera(f.camera)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.stage = 0
        self.phase = "mount"
        self.next_at = 0
        self.started = time.monotonic()
        self.generation = 0
        self.busy = False
        self.finished = False
        self.last_tick = self.started
        self.retiring = None
        self.report = dict(
            result="running", events=[], heartbeat_gaps=[], geometry_pose_overlap=False, scope=self.scope
        )
        hou.ui.addEventLoopCallback(self.tick)

    def edit(self, parm, value):
        self.parm = parm
        self.text = str(value)
        with hou.undos.disabler():
            self.settings.setCurrentNode(parm.node())
            self.settings.setIsCurrentTab()
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus(QtCore.Qt.OtherFocusReason)
        QtGui.QCursor.setPos(self.window.mapToGlobal(self.window.rect().center()))
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.25

    def mutate(self):
        f = self.f
        self.before_serial = self.panel.entry.change_serial
        if self.stage == 1:
            self.edit(self.scale_parm, 0.5)
            return
        if self.stage == 2:
            self.edit(self.broken_parm, self.broken_value)
            return
        if self.stage == 3:
            self.edit(f.camera.parm("tx"), 0.05)
            return
        if self.stage == 4:
            self.edit(self.broken_parm, 0)
            return
        if self.stage == 5:
            self.edit(f.shader.parmTuple("color")[0], 0.4)
            return
        if self.stage == 6:
            self.edit(f.obj.parm("tx"), 0.05)
            return
        if self.stage == 7:
            self.edit(f.camera.parm("tx"), 0.1)
            return
        if self.stage == 8:
            self.edit(f.motion.parm("test_rows"), 723)
            return
        if self.stage == 9:
            f.camera.parm("tx").setExpression(self.camera_animation, hou.exprLanguage.Hscript)
        elif self.stage in (10, 11, 12, 13):
            hou.setFrame(self.stage - 8)
        self.phase = "wait"
        self.next_at = time.monotonic() + 0.1

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["heartbeat_gaps"].append(now - self.last_tick)
            self.last_tick = now
            assert now - self.started < 300, (self.stage, self.phase, self.report.get("current"))
            if self.retiring is not None:
                status = self.retiring.status()
                assert status["state"] != "failed", status
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                self.report["closed"] = status
                self.finish()
                return
            if now < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                return
            if self.phase == "focus":
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "keys"
                self.next_at = now + 0.2
                return
            if self.phase == "keys":
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.text)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = "confirm"
                self.next_at = now + 0.2
                return
            if self.phase == "confirm":
                assert abs(self.parm.eval() - float(self.text)) < 0.00001
                self.phase = "wait"
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            frame = entry.session.frame_info()
            self.report["current"] = dict(
                stage=self.stage,
                status=status,
                frame=frame,
                serial=entry.change_serial,
                applied=entry.applied_serial,
                observed_time=entry.observed_time,
                host_time=hou.time(),
                camera_tx=self.f.camera.evalParm("tx"),
                object_tx=self.f.obj.evalParm("tx"),
                speed=self.scale_parm.eval(),
            )
            self.path.write_text(json.dumps(self.report, indent=2))
            if self.stage == 8 and status.get("capture_pending") and not self.report["geometry_pose_overlap"]:
                self.f.camera.parm("tx").set(0.12)
                self.f.obj.parm("tx").set(0.08)
                self.report["geometry_pose_overlap"] = True
                return
            if (
                status.get("capture_pending")
                or status.get("pending_update")
                or not frame
                or not frame["complete"]
                or frame["generation"] <= self.generation
            ):
                return
            if self.stage and (entry.applied_serial != entry.change_serial or entry.observed_time != hou.time()):
                return
            if 0 < self.stage < 10 and entry.change_serial <= self.before_serial:
                return
            if frame["generation"] != status["generation"] or not frame.get("current_scene"):
                return
            if (
                self.panel._live_generation != frame["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != frame["sequence"]
            ):
                return
            # Freeze one coherent image. A newer progressive/preview frame can
            # arrive between reading its header and acquiring the pixel buffer.
            image_frame = entry.session.frame()
            if (
                not image_frame
                or not image_frame["complete"]
                or image_frame["generation"] != frame["generation"]
                or image_frame["sequence"] != frame["sequence"]
            ):
                return
            if (
                self.stage == 8
                and not self.report["geometry_pose_overlap"]
                and not self.report.get("geometry_pose_after_completion")
            ):
                self.f.camera.parm("tx").set(0.12)
                self.f.obj.parm("tx").set(0.08)
                self.report["geometry_pose_after_completion"] = True
                return
            assert (
                status["render_device_ids"] == [self.f.device]
                and status["geometry_resources"] == 1
                and status["object_resources"] == 16
            ), status
            self.check_status(status)
            assert bool(status["source_diagnostics"]) == (self.stage in (2, 3)), status["source_diagnostics"]
            assert len(registry.entries()) == 1 and entry.views == 1
            expected = 1.0 if self.stage in (2, 3) else 0.4 if self.stage == 0 else 0.8
            self.f.color = np.array((0.4, 0.2, 0.05) if self.stage >= 5 else (0.8, 0.2, 0.05))
            x = -1 + self.f.obj.evalParm("tx") - (self.f.camera.evalParm("tx") - self.animated_position())
            measured = self.f.coverage(image_frame, x, -1)
            pixels = self.f.pixels(image_frame)
            image = self.path.with_name(self.path.stem + f".stage-{self.stage:02d}.png")
            output = oiio.ImageOutput.create(str(image))
            assert output.open(str(image), oiio.ImageSpec(self.f.width, self.f.height, 3, oiio.UINT8))
            assert output.write_image((np.clip(np.flipud(pixels[:, :, :3]), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8))
            assert output.close()
            self.report["current"].update(probe_x=x, measured=measured, expected=expected, image=str(image))
            self.path.write_text(json.dumps(self.report, indent=2))
            assert abs(measured - expected) < 0.085, (self.stage, measured, expected)
            if self.stage == 0:
                self.pid = status["renderer_pid"]
            assert self.pid == status["renderer_pid"]
            if self.stage in self.retained_stages:
                assert status["geometry_conversions"] == self.conversions, (self.stage, status)
            if self.stage in self.changed_stages:
                assert status["geometry_conversions"] > self.conversions, (self.stage, status)
            self.conversions = status["geometry_conversions"]
            self.generation = frame["generation"]
            self.report["events"].append(
                dict(
                    stage=self.stage,
                    frame=hou.frame(),
                    coverage=measured,
                    expected=expected,
                    status=status,
                    canvas=dict(self.panel.canvas.frame_metadata),
                )
            )
            self.path.write_text(json.dumps(self.report, indent=2))
            self.stage += 1
            if self.stage == 14:
                self.retiring = entry.session
                self.panel.dispose()
                registry.close_all()
                return
            self.mutate()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


if __name__ == "__main__":
    workflow = Workflow()
