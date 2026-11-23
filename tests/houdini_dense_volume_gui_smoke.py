# SPDX-License-Identifier: Apache-2.0
"""One viewer: native parameter entry, dense-field repair and retained heavy instances."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
from cycles_houdini import registry
from dense_volume_fixture import CASES, fixture, check


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_DENSE_VIEWER") == "native"
        self.rop, self.source, self.assignment, dest = fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], scale=2)
        self.heavy = hou.node("/obj").createNode("geo", "retained_dense_instances", run_init_scripts=False)
        self.torus = self.heavy.createNode("torus", "million_triangle_prototype")
        self.torus.setParms(dict(type=0, rows=512, cols=1024))
        targets = self.heavy.createNode("grid", "twelve_targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((0, 0, 20))
        copies = self.heavy.createNode("copytopoints", "shared_instances")
        copies.setInput(0, self.torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        self.heavy.parm("shop_materialpath").set("/mat/dense_recovery_sentinel")
        self.rop.parm("vobject").set(self.rop.evalParm("vobject") + " " + self.heavy.path())
        camera = hou.node(self.rop.evalParm("camera"))
        self.steps = []
        for index, case in enumerate(CASES):
            edits = [(self.source.parm("mode"), case.mode), (self.assignment.parm("binding"), case.binding)]
            self.steps.append(dict(case=case, label=f"field-{index}", edits=edits))
            if index == 7:
                for label, parm, value in (
                    ("camera-move", camera.parm("tx"), 0.02),
                    ("camera-return", camera.parm("tx"), 0),
                    ("instance-move", self.heavy.parm("tx"), 0.5),
                    ("instance-return", self.heavy.parm("tx"), 0),
                ):
                    self.steps.append(dict(case=case, label=label, edits=[(parm, value)], navigation=True))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 600))
        self.settings.setPin(True)
        self.settings.setCurrentNode(self.source)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            events=[],
            render_clicks=1,
            scope=__doc__,
            physical_mouse_certification=False,
            heavy_scope="12 behind-camera shared instances retain a million-triangle prototype; ownership stress, not a ray-performance benchmark",
        )
        self.index = self.generation = 0
        self.phase = "mount"
        self.began = time.monotonic()
        self.next_at = 0
        self.saved_at = 0
        self.busy = self.finished = False
        self.entry = self.session = None
        self.previous = None
        hou.hipFile.save(str(dest / ("dense-recovery-" + self.report["viewer"] + ".hiplc")))
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def begin_edit(self):
        self.edits = [
            (parm, value) for parm, value in self.steps[self.index]["edits"] if abs(float(parm.eval()) - value) > 1e-8
        ]
        assert self.edits, self.steps[self.index]["label"]
        self.focus_edit()

    def focus_edit(self):
        self.parm, self.value = self.edits[0]
        self.settings.setCurrentNode(self.parm.node())
        self.settings.setIsCurrentTab()
        for folder, index in zip(self.parm.containingFolderSetParmTuples(), self.parm.containingFolderIndices()):
            folder[0].set(1 if folder.parmTemplate().folderType() == hou.folderType.Collapsible else index)
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus()
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.2

    def ready(self):
        entry = registry.get(f"rop:{self.rop.sessionId()}")
        if entry is None:
            return False
        self.entry = entry
        status = entry.session.status()
        info = entry.session.frame_info()
        assert not entry.error and status["state"] != "failed", (entry.error, status)
        if (
            not info
            or not info["complete"]
            or info["generation"] <= self.generation
            or info["generation"] != status["generation"]
            or status.get("capture_pending")
            or status.get("pending_update")
        ):
            return False
        if not self.native and (
            self.panel._live_generation != info["generation"]
            or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
        ):
            return False
        frame = entry.session.frame()
        step = self.steps[self.index]
        observation = check(frame, status, step["case"], geometry_extra=1, object_extra=12)
        assert status["packed_instances"] == 12 and len(registry.entries()) == 1, status
        if self.native:
            if not entry.helper_pid or self.tab.imageResolution() != (160, 96):
                return False
            plane = frame["planes"]["combined"]
            image = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])
            try:
                if any(
                    not np.allclose(self.tab.pixel("C", x, y)[:3], image[y, x, :3], atol=2e-4, rtol=0)
                    for x, y in ((80, 48), (140, 48), (8, 8))
                ):
                    return False
            except hou.OperationFailed:
                return False
        else:
            assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        cooks = self.torus.cookCount()
        if self.previous:
            assert status["renderer_pid"] == self.previous["renderer_pid"] and cooks == self.heavy_cooks, status
            if step.get("navigation"):
                assert all(
                    status[key] == self.previous[key]
                    for key in ("dense_field_conversions", "geometry_conversions", "extractions")
                ), status
        self.generation = info["generation"]
        self.report["events"].append(
            dict(
                label=step["label"],
                mode=step["case"].mode,
                binding=step["case"].binding,
                status=status,
                heavy_cooks=cooks,
                **observation,
            )
        )
        self.previous = status
        self.heavy_cooks = cooks
        return True

    def close(self, error=None):
        if self.phase == "closing":
            return
        self.error = error
        if self.entry:
            self.session = self.entry.session
        if self.native:
            self.tab.killRender()
        elif self.panel:
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
            self.panel.dispose()
        registry.close_all()
        self.phase = "closing"
        self.close_deadline = time.monotonic() + 20

    def finish(self):
        self.finished = True
        hou.ui.removeEventLoopCallback(self.tick)
        self.report.update(result="failed" if self.error else "passed", error=self.error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["phase"] = self.phase
            if now - self.saved_at > 0.5:
                self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
                self.saved_at = now
            if self.phase == "closing":
                if self.session:
                    status = self.session.status()
                    if status["state"] != "closed" and now < self.close_deadline:
                        return
                    self.report["closed"] = status
                    if status["state"] != "closed" or any(
                        status[k]
                        for k in ("owned_geometry_bytes", "device_allocation_bytes", "retained_dense_voxel_bytes")
                    ):
                        self.error = (self.error or "") + " Resources not retired after close: " + repr(status)
                self.finish()
                return
            assert now - self.began < 300, (self.phase, self.index)
            if now < self.next_at:
                return
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            elif self.phase == "focus":
                assert self.parm.tuple() in self.settings.visibleParms(), self.parm.path()
                self.settings.scrollTo(self.parm, hou.scrollPosition.Center)
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "type"
                self.next_at = now + 0.2
            elif self.phase == "type":
                for _ in range(self.parm.componentIndex()):
                    QtTest.QTest.keyClick(QtWidgets.QApplication.focusWidget() or self.window, QtCore.Qt.Key_Tab)
                focus = QtWidgets.QApplication.focusWidget() or self.window
                self.phase = "sending"
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, str(self.value))
                self.phase = "commit"
                self.next_at = now + 0.15
            elif self.phase == "commit":
                QtTest.QTest.keyClick(QtWidgets.QApplication.focusWidget() or self.window, QtCore.Qt.Key_Return)
                self.commit_deadline = time.monotonic() + 5
                self.phase = "confirm"
                self.next_at = now + 0.15
            elif self.phase == "confirm":
                matches = abs(float(self.parm.eval()) - self.value) < 1e-8
                assert matches or now < self.commit_deadline, (self.parm.path(), self.parm.eval(), self.value)
                if not matches:
                    return
                self.edits.pop(0)
                if self.edits:
                    self.focus_edit()
                else:
                    self.phase = "wait"
                    self.next_at = now + 0.2
            elif self.phase == "wait" and self.ready():
                self.index += 1
                if self.index == len(self.steps):
                    target = self.path.with_name("dense-viewer.png")
                    self.report["viewer_capture_saved"] = (
                        hou.qt.floatingPanelWindow(self.tab.floatingPanel()).grab().save(str(target))
                    )
                    self.report["viewer_capture"] = str(target)
                    self.close()
                else:
                    self.begin_edit()
        except BaseException:
            self.close(traceback.format_exc())
        finally:
            self.busy = False


if __name__ == "__main__":
    workflow = Workflow()
