# SPDX-License-Identifier: Apache-2.0
"""Break and repair packed files through native parameter edits in one running IPR."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry

exec((Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/packed_source_recovery_fixture.py").read_text())


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[])
        self.obj, self.out, self.edit = packed_source_fixture()
        cam = hou.node("/obj").createNode("cam", "camera")
        cam.setParms(dict(tz=6, projection=1, orthowidth=6))
        self.rop = hou.node("/out").createNode("cycles_ipr", "packed_source_recovery")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=cam.path(),
                resx=192,
                resy=96,
                samples=16,
                ipr_samples=16,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                use_material_assignments=0,
                material_kind="emission",
                point_radius=0.2,
                curve_width=0.1,
            )
        )
        self.rop.parmTuple("basecolor").set((0.2, 0.7, 0.1))
        self.rop.parmTuple("background").set((0, 0, 0))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setPin(True)
        self.settings.setCurrentNode(self.edit)
        self.panel = None
        self.stage = "mount"
        self.started = time.monotonic()
        self.next_at = 0
        self.generation = 0
        self.busy = False
        self.modes = [0, 1, 0, 3, 0]
        self.index = 0
        self.nested = 0
        example = artifacts_root() / "examples/packed-source-recovery/packed-source-recovery.hiplc"
        hou.hipFile.save(str(example))
        hou.ui.addEventLoopCallback(self.tick)

    def begin_edit(self):
        self.settings.setIsCurrentTab()
        window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        window.show()
        window.raise_()
        window.activateWindow()
        self.stage = "focus"
        self.next_at = time.monotonic() + 0.25

    def ready(self):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        assert not entry.error and status["state"] != "failed", (entry.error, status)
        info = entry.session.frame_info()
        if (
            status.get("capture_pending")
            or not info
            or not info["complete"]
            or info["generation"] <= self.generation
            or self.panel._live_generation != info["generation"]
        ):
            return False
        # A generation's first coarse preview is not its completed displayed image.
        if self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]:
            return False
        self.generation = info["generation"]
        pixels = np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
        broken = self.modes[self.index] in (1, 3)
        assert_component_pixels(pixels, broken)
        issues = status["source_diagnostics"]
        assert len(issues) == int(broken), (self.index, status)
        if broken:
            assert issues[0]["code"] == "geometry.packed_unavailable"
        if self.index == 0:
            self.baseline = pixels
            self.objects = status["object_resources"]
            assert self.objects == 6, status
        if broken:
            assert status["object_resources"] == self.objects - 1
            if self.index == 1:
                self.broken = pixels
            else:
                assert np.array_equal(pixels, self.broken)
        else:
            assert np.array_equal(pixels, self.baseline) and status["object_resources"] == self.objects
        assert len(registry.entries()) == 1 and entry.views == 1
        assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        self.report["events"].append(
            dict(nested=self.nested, mode=self.modes[self.index], generation=self.generation, diagnostics=issues)
        )
        self.panel.grab().save(str(self.path.with_name("%s-%d-%d.png" % (self.path.stem, self.nested, self.index))))
        return True

    def tick(self):
        # HOM cooks and Qt keyboard delivery can run nested event loops.
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 150, (self.stage, self.report)
            if time.monotonic() < self.next_at:
                return
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "wait"
            elif self.stage == "focus":
                self.settings.moveFocusTo("mode")
                self.stage = "type"
                self.next_at = time.monotonic() + 0.2
            elif self.stage == "type":
                self.stage = "sending"
                focus = QtWidgets.QApplication.focusWidget()
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, str(self.modes[self.index]))
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.stage = "wait"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "wait":
                assert self.edit.evalParm("mode") == self.modes[self.index], self.edit.evalParm("mode")
                if not self.ready():
                    return
                self.index += 1
                if self.index == len(self.modes):
                    if self.nested == 1:
                        QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                        self.finish()
                        return
                    self.nested = 1
                    self.index = 0
                    self.out.parm("input").set(1)
                else:
                    self.begin_edit()
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
