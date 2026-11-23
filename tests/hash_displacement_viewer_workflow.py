# SPDX-License-Identifier: Apache-2.0
"""Actual Houdini attribute/socket edits and repairs with one live viewer.

Queued HOM camera/object navigation checks continued delivery and retained
geometry. Heartbeats are observations, not physical input-to-paint budgets.
"""

import json
import os
import time
import traceback
from pathlib import Path

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from hash_displacement_fixture import Fixture, plane


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_TRANSPORT_VIEWER") == "native"
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.f = Fixture(json.loads(device) if device.startswith("[") else device)
        # Height changes must also visibly change the displayed silhouette.
        self.f.camera.parm("projection").set(0)
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.steps = []
        for case in ("scalar", "vector", "float2", "uv", "color", "packed", "parameter", "parameter_vector"):
            for bits in (0, 0x80000000, 0):
                self.steps.append((case, bits, False, False))
            if not case.startswith("parameter"):
                self.steps.extend(((case, 0x7FC00000, True, False), (case, 0, False, False)))
        self.steps.extend((("scalar", 0x80000000, False, True), ("scalar", 0, False, True)))
        self.index = self.generation = self.navigation_pending = self.navigation_events = 0
        self.panel = self.entry = self.session = None
        self.phase, self.busy, self.finished = "mount", False, False
        self.started = self.edited_at = self.beat_at = time.monotonic()
        self.heartbeats = []
        self.report = dict(result="running", scope=__doc__, events=[], viewer="native" if self.native else "cycles")
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setInterval(20)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def beat(self):
        now = time.monotonic()
        self.heartbeats.append(now - self.beat_at)
        self.beat_at = now

    def edit(self):
        case, bits, _, navigate = self.steps[self.index]
        if self.f.case != case:
            self.f.configure(case)
        self.f.edit(bits)
        self.edited_at = time.monotonic()
        if navigate:
            for index, delay in enumerate((10, 30, 50)):
                self.navigation_pending += 1

                def move(index=index):
                    if self.finished or self.phase == "closing":
                        return
                    try:
                        self.f.camera.parm("tx").set(0.02 * (index + 1))
                        self.f.obj.parm("tx").set(0.015 * (index + 1))
                        self.navigation_pending -= 1
                        self.navigation_events += 1
                        self.edited_at = time.monotonic()
                    except BaseException:
                        self.close(traceback.format_exc())

                QtCore.QTimer.singleShot(delay, move)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and time.monotonic() < self.deadline:
                    return
                if status:
                    assert (
                        status["state"] == "closed"
                        and status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0
                    ), status
                self.heartbeat.stop()
                self.report.update(
                    result="failed" if self.error else "passed",
                    error=self.error,
                    closed=status,
                    navigation_events=self.navigation_events,
                    heartbeats=dict(
                        count=len(self.heartbeats),
                        maximum=max(self.heartbeats, default=0),
                        p95=float(np.percentile(self.heartbeats, 95)) if self.heartbeats else 0,
                    ),
                )
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.started < 600, (self.phase, self.index)
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
                index=self.index,
                change_serial=entry.change_serial,
                applied_serial=entry.applied_serial,
                requested_serial=entry.requested_serial,
                status=status,
            )
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                self.navigation_pending
                or entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or time.monotonic() - self.edited_at < 0.35
            ):
                return
            if (
                status["state"] != "complete"
                or status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] < self.generation
                or info["generation"] != status["generation"]
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            case, bits, warning, navigate = self.steps[self.index]
            self.f.verify_source()
            frame = entry.session.frame()
            height = self.f.check(frame, interior=True)
            expected = 0 if warning else (0.45130395889282227 if bits == 0x80000000 else 0.29121291637420654)
            assert abs(height - expected) < 2e-5, (case, bits, height, expected)
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            assert len(registry.entries()) == 1
            if self.native:
                if not entry.helper_pid:
                    return
                try:
                    pixels = plane(frame, "combined")
                    for x in (12, 16, 20, 32, 44, 48, 52):
                        if not np.allclose(self.tab.pixel("C", x, 32)[:3], pixels[32, x, :3], atol=3e-4, rtol=0):
                            return
                except hou.OperationFailed:
                    return
            else:
                assert (
                    entry.views == 1 and not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                )
            if self.report["events"]:
                assert status["renderer_pid"] == self.report["events"][0]["status"]["renderer_pid"], status
            self.generation = info["generation"]
            self.report["events"].append(
                dict(
                    case=case,
                    bits=bits,
                    warning=warning,
                    height=height,
                    status=status,
                    navigation_events=self.navigation_events,
                )
            )
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                assert self.navigation_events == 6
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
            else:
                self.edit()
        except BaseException:
            if self.phase == "closing":
                self.heartbeat.stop()
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
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
    workflow = Workflow()  # Keep Qt bound slots alive beyond the runpy namespace.
