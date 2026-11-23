# SPDX-License-Identifier: Apache-2.0
"""One live viewer: native nonlinear fields, scoped repairs and retained heavy data."""

import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from houdini_geometry_key_bindings_gui_smoke import Workflow as ViewerWorkflow
from tapered_volume_fixture import Fixture


class Workflow(ViewerWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_TAPERED_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], heavy=True)
        self.f.rop.setCurrent(True, clear_all_selected=True)
        self.key = "rop:" + str(self.f.rop.sessionId())
        self.steps = self.f.steps()
        self.authored = self.f.materials()
        self.entry = self.panel = self.session = None
        self.index = self.generation = self.heartbeats = 0
        self.phase = "mount"
        self.busy = self.finished = False
        self.started = self.edited_at = time.monotonic()
        self.saved_at = 0
        self.report = dict(
            result="running",
            scope=__doc__,
            physical_input_certification=False,
            viewer="native" if self.native else "cycles",
            events=[],
            render_clicks=1,
            heavy_scope="12 behind-camera shared instances retain a million-triangle prototype; ownership, not a ray benchmark",
        )
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
        self.callback = self.tick
        self.heartbeat = QtCore.QTimer(hou.qt.mainWindow())
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start(20)
        hou.ui.addEventLoopCallback(self.callback)
        self.persist_wait()
        if self.native:
            self.tab.startRender()

    def cooks(self):
        return dict(
            source=self.f.control.cookCount(), heavy=self.f.copies.cookCount(), prototype=self.f.prototype.cookCount()
        )

    def native_matches(self, pixels):
        if not self.entry.helper_pid or self.tab.imageResolution() != (96, 64):
            return False
        try:
            return all(
                np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=3e-4, rtol=0)
                for x, y in ((48, 32), (86, 32), (4, 4), (30, 24), (60, 40))
            )
        except hou.OperationFailed:
            return False

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            self.persist_wait()
            now = time.monotonic()
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and now < self.deadline:
                    return
                if status:
                    assert status["state"] == "closed" and all(
                        status[k] == 0
                        for k in ("owned_geometry_bytes", "device_allocation_bytes", "retained_dense_voxel_bytes")
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                self.detach()
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert now - self.started < 750, self.report
            assert now - self.edited_at < (180 if self.index == 0 else 50), self.report
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            self.entry = entry = registry.get(self.key)
            if entry is None:
                return
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
                status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or info["generation"] <= self.generation
            ):
                return
            if not self.native:
                metadata = getattr(self.panel.canvas, "frame_metadata", None) or {}
                if self.panel._live_generation != info["generation"] or metadata.get("sequence") != info["sequence"]:
                    return
            frame = entry.session.frame()
            self.f.assert_status(status, heavy=True)
            measured = self.f.assert_pixels(frame)
            assert (
                self.f.materials() == self.authored
                and len(registry.entries()) == 1
                and status["packed_instances"] == 12
            ), status
            pixels = self.f.pixels(frame)
            if self.native:
                if not self.native_matches(pixels):
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                warnings = [d for d in status["source_diagnostics"] if d.get("severity", "warning") != "info"]
                headline = self.panel.info.text().split("\n", 1)[0]
                # Presentation may lead the status-label poll by one timer tick.
                # Accept only after both show the same accepted diagnostics.
                if ("source warning(s)" in headline) != bool(warnings):
                    return
                if status["source_diagnostics"] and not warnings and "Resampled" not in self.panel.info.toolTip():
                    return
            cooks = self.cooks()
            label = self.steps[self.index][0]
            if self.report["events"]:
                first, previous = self.report["events"][0], self.report["events"][-1]
                assert status["renderer_pid"] == first["status"]["renderer_pid"], status
                assert self.heartbeats > previous["heartbeats"], (self.heartbeats, previous)
                assert (
                    cooks["heavy"] == first["cooks"]["heavy"] and cooks["prototype"] == first["cooks"]["prototype"]
                ), (cooks, first)
                if self.f.navigation:
                    assert cooks["source"] == previous["cooks"]["source"], (cooks, previous)
                    assert all(
                        status[k] == previous["status"][k]
                        for k in ("dense_field_conversions", "geometry_conversions", "extractions")
                    ), status
                    assert status["source_diagnostics"] == previous["status"]["source_diagnostics"], status
            self.report["events"].append(
                dict(
                    label=label,
                    status=status,
                    frame_info=info,
                    cooks=cooks,
                    measured=measured,
                    authored_material_unchanged=True,
                    heartbeats=self.heartbeats,
                    state_seconds=now - self.edited_at,
                )
            )
            self.generation = info["generation"]
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.steps[self.index][1]()
            assert self.f.materials() == self.authored
            self.edited_at = time.monotonic()
        except BaseException:
            if self.phase == "closing":
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                self.detach()
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False


def start():
    global workflow
    workflow = Workflow()


if __name__ == "__main__":
    start()
