# SPDX-License-Identifier: Apache-2.0
"""Automatic captures preserve identical preview work; explicit Refresh restarts."""

import json
import os
import time
import traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from packed_deformation_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop = self.f.rop(self.f.pack, interactive_samples=512)
        self.scene = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer, size=(600, 500))
        self.scene.curViewport().setCamera(self.f.camera)
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.stage = 0
        self.started = time.monotonic()
        self.busy = self.finished = False
        self.retiring = None
        self.report = dict(result="running", events=[], scope=__doc__)
        hou.ui.addEventLoopCallback(self.tick)

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 100, (self.stage, self.report.get("current"))
            if self.retiring is not None:
                status = self.retiring.status()
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                self.report["closed"] = status
                self.finish()
                return
            if self.stage == 0:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = 1
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            self.report["current"] = dict(stage=self.stage, status=status)
            assert not entry.error and status["state"] != "failed" and not status["error"], status
            frame = entry.session.frame_info()
            if (
                status.get("capture_pending")
                or status.get("pending_update")
                or not frame
                or not frame["complete"]
                or not frame.get("current_scene")
                or frame["generation"] != status["generation"]
            ):
                return
            if self.stage > 1 and status["generation"] <= self.before["generation"]:
                return
            assert status["render_device_ids"] == [self.f.device], status
            assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1 and entry.views == 1
            if self.stage == 1:
                self.pid = status["renderer_pid"]
                self.geometry_conversions = status["geometry_conversions"]
            else:
                assert (
                    status["renderer_pid"] == self.pid and status["geometry_conversions"] == self.geometry_conversions
                ), status
            if self.stage == 2:
                assert status["unchanged_reuses"] > self.before["unchanged_reuses"], status
                image = entry.session.frame()
                assert abs(self.f.coverage(image, 0.05) - 0.4) < 0.07
            elif self.stage in (3, 4):
                assert status["render_revision"] > self.before["render_revision"], status
                assert status["unchanged_reuses"] == self.before["unchanged_reuses"], status
            elif self.stage == 5:
                assert status["render_revision"] == self.before["render_revision"], status
                assert status["unchanged_reuses"] > self.before["unchanged_reuses"], status
            self.report["events"].append(dict(stage=self.stage, status=status))
            self.path.write_text(json.dumps(self.report, indent=2))
            self.before = status
            if self.stage == 1:
                self.f.obj.parm("tx").set(0.05)
            elif self.stage == 2:
                QtTest.QTest.mouseClick(self.panel.refresh_button, QtCore.Qt.LeftButton)
            elif self.stage == 3:
                self.panel.queue_render(reuse_if_unchanged=True)
                self.panel.queue_render()
                assert self.panel._force_render_pending
            elif self.stage == 4:
                self.panel.queue_render(reuse_if_unchanged=True)
            elif self.stage == 5:
                self.retiring = entry.session
                self.panel.dispose()
                registry.close_all()
                return
            self.stage += 1
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
