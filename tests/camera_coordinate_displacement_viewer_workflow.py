# SPDX-License-Identifier: Apache-2.0
"""One viewer, camera/projection edits and displacement disconnect/repair.

The numeric oracle is the height of independent pre-displaced triangles, not
another invocation of the displacement shader. HOM edits are event-loop edits;
this is not physical mouse or input-latency certification.
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
from camera_coordinate_displacement_fixture import Fixture, plane


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_COORDINATE_VIEWER") == "native"
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.f = Fixture(json.loads(device) if device.startswith("[") else device)
        # The numeric oracle creates a second ROP. Keep that helper out of
        # Follow Selection before Houdini delivers the pane's initial path.
        self.f.reference_rop.setSelected(False)
        self.f.rop.setCurrent(True, clear_all_selected=True)
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.steps = [
            ("camera data", lambda: None, None, True),
            ("depth camera translation", lambda: self.f.camera.parm("tz").set(5.6), 1, True),
            ("depth lens only", lambda: self.f.camera.parm("orthowidth").set(4.1), 0, True),
            ("camera coordinate", lambda: self.f.configure("camera_coordinate"), None, True),
            ("camera coordinate translation", lambda: self.f.camera.parm("tz").set(5.8), 1, True),
            ("window", lambda: self.f.configure("window"), None, True),
            ("window camera translation", lambda: self.f.camera.parm("ty").set(0.3), 1, True),
            ("window orthographic width", lambda: self.f.camera.parm("orthowidth").set(4.1), 1, True),
            ("window pixel aspect", lambda: self.f.rop.parm("pixel_aspect").set(1.2), 1, True),
            ("window projection", lambda: self.f.camera.parm("projection").set(0), 1, True),
            ("window focal length", lambda: self.f.camera.parm("focal").set(42), 1, True),
            ("window clip only", lambda: self.f.camera.parm("near").set(0.2), 0, True),
            ("disconnected displacement", lambda: self.connect(False), 1, False),
            ("camera with disconnected displacement", lambda: self.f.camera.parm("ty").set(-0.25), 0, False),
            ("repaired displacement", lambda: self.connect(True), 1, True),
            ("restored window", lambda: self.f.configure("window"), None, True),
        ]
        self.index, self.generation = 0, 0
        self.panel = self.entry = self.session = None
        self.phase, self.busy, self.finished = "mount", False, False
        self.started = self.edited_at = time.monotonic()
        self.saved_at = self.heartbeats = 0
        self.report = dict(result="running", scope=__doc__, events=[], viewer="native" if self.native else "cycles")
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
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setInterval(20)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()
        hou.ui.addEventLoopCallback(self.tick)
        self.persist_wait()
        if self.native:
            self.tab.startRender()

    def connect(self, enabled):
        output = self.f.materials[0].node("surface_output")
        if enabled:
            output.setNamedInput("displacement", self.f.materials[0].node("offset"), "displacement")
        else:
            output.setInput(output.inputIndex("displacement"), None)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def beat(self):
        self.heartbeats += 1

    def persist_wait(self):
        now = time.monotonic()
        if now - self.saved_at < 1:
            return
        self.saved_at = now
        self.report.update(
            phase=self.phase,
            index=self.index,
            label=self.steps[min(self.index, len(self.steps) - 1)][0],
            elapsed_seconds=now - self.started,
            state_seconds=now - self.edited_at,
            heartbeats=self.heartbeats,
            registry_keys=[entry.key for entry in registry.entries()],
            panel_rop=self.panel.rop_path.currentData() if self.panel else None,
            panel_info=self.panel.info.text() if self.panel else None,
            displayed=dict(getattr(self.panel.canvas, "frame_metadata", {})) if self.panel else None,
        )
        self.save()

    def detach(self):
        self.heartbeat.stop()
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            self.persist_wait()
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and time.monotonic() < self.deadline:
                    return
                if status:
                    assert (
                        status["state"] == "closed"
                        and status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                self.detach()
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.started < 360, (self.phase, self.index)
            assert time.monotonic() - self.edited_at < 45, self.report
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
                frame_info=info,
            )
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
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
            label, _, rebuilds, enabled = self.steps[self.index]
            assert not status["source_diagnostics"] and not status["error"], status
            frame = entry.session.frame()
            errors = self.f.check(frame, target_enabled=enabled)
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
                assert (
                    entry.views == 1 and not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                )
            if self.report["events"]:
                previous = self.report["events"][-1]["status"]
                assert status["renderer_pid"] == previous["renderer_pid"], status
                assert status["geometry_conversions"] == previous["geometry_conversions"], status
                if rebuilds is not None:
                    assert status["displacement_rebuilds"] == previous["displacement_rebuilds"] + rebuilds, (
                        label,
                        previous,
                        status,
                    )
            self.generation = info["generation"]
            self.report["events"].append(dict(label=label, status=status, **errors))
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
            else:
                self.steps[self.index][1]()
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
