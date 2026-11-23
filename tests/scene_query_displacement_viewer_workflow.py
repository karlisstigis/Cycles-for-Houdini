# SPDX-License-Identifier: Apache-2.0
"""One viewer: query faults, independent height and live graph repairs."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from scene_query_displacement_fixture import Fixture, QUERIES, plane


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_QUERY_VIEWER") == "native"
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.f = Fixture(json.loads(device) if device.startswith("[") else device)
        self.f.reference_rop.setSelected(False)
        self.f.rop.setCurrent(True, clear_all_selected=True)
        self.steps = [
            (kind, mode, method)
            for kind in QUERIES
            for mode, method in (
                ("valid", "true"),
                ("whole", "true"),
                ("add", "true"),
                ("vector_add", "both"),
                ("coupled", "true"),
                ("valid", "true"),
                ("disabled", "true"),
                ("bump", "bump"),
            )
        ]
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.index = 0
        self.panel = self.entry = self.session = None
        self.busy = self.finished = False
        self.phase = "mount"
        self.started = self.edited_at = time.monotonic()
        self.saved_at = 0
        self.report = dict(result="running", viewer="native" if self.native else "cycles", scope=__doc__, events=[])
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setPin(True)
            self.tab.setCurrentNode(self.f.rop, pick_node=False)
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.timer = QtCore.QTimer()
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()
        if self.native:
            self.tab.startRender()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and now < self.deadline:
                    return
                if status:
                    assert (
                        status["state"] == "closed"
                        and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0
                    ), status
                self.timer.stop()
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            if now - self.saved_at >= 1:
                self.saved_at = now
                self.report.update(
                    phase=self.phase,
                    index=self.index,
                    state_seconds=now - self.edited_at,
                    registry_keys=[e.key for e in registry.entries()],
                    panel_rop=self.panel.rop_path.currentData() if self.panel else None,
                    panel_info=self.panel.info.text() if self.panel else None,
                    displayed=dict(getattr(self.panel.canvas, "frame_metadata", {})) if self.panel else None,
                )
                self.save()
            assert now - self.started < 150 and now - self.edited_at < 45, self.report
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            entry = registry.get(self.key)
            if entry is None:
                return
            self.entry = entry
            status, info = entry.session.status(), entry.session.frame_info()
            self.report["last_wait_state"] = dict(
                change_serial=entry.change_serial,
                applied_serial=entry.applied_serial,
                requested_serial=entry.requested_serial,
                status=status,
                frame_info=info,
            )
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if entry.change_serial != entry.applied_serial or entry.requested_serial or now - self.edited_at < 0.35:
                return
            if (
                status["state"] != "complete"
                or status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            frame = entry.session.frame()
            measured = self.f.check(frame, status)
            assert len(registry.entries()) == 1
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (self.f.width, self.f.height):
                    return
                pixels = plane(frame, "combined")
                locations = np.argwhere(pixels[..., 3] > 0.999)
                try:
                    for y, x in locations[:: max(1, len(locations) // 6)]:
                        if not np.allclose(
                            self.tab.pixel("C", int(x), int(y))[:3], pixels[y, x, :3], atol=3e-4, rtol=0
                        ):
                            return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            if self.report["events"]:
                assert status["renderer_pid"] == self.report["events"][0]["status"]["renderer_pid"], status
            self.report["events"].append(dict(case=self.f.case, status=status, **measured))
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
            else:
                self.f.configure(*self.steps[self.index])
                self.edited_at = time.monotonic()
        except BaseException:
            if self.phase == "closing":
                self.timer.stop()
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        self.error = error
        self.session = self.entry.session if self.entry else None
        if self.native:
            self.tab.killRender()
        elif self.panel:
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
            self.panel.dispose()
        registry.close_all()
        self.phase = "closing"
        self.deadline = time.monotonic() + 20


def start():
    global workflow
    workflow = Workflow()
