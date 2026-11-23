# SPDX-License-Identifier: Apache-2.0
"""HOM edits and displayed pixels in one retained viewer; no physical-input claim."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from geometry_key_binding_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_KEY_BINDINGS_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], heavy=True)
        self.f.rop.setCurrent(True, clear_all_selected=True)
        self.key = "rop:" + str(self.f.rop.sessionId())
        self.steps = self.f.steps()
        self.authored = self.f.authored()
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

    def beat(self):
        self.heartbeats += 1

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

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
        )
        self.save()

    def cooks(self):
        source = self.f.heavy.renderNode()
        return dict(mesh=self.f.sop.cookCount(), heavy=source.cookCount(), prototype=source.inputs()[0].cookCount())

    def native_matches(self, pixels):
        if not self.entry.helper_pid or self.tab.imageResolution() != (96, 96):
            return False
        shift = self.f.obj.parm("tx").eval() - self.f.camera.parm("tx").eval()
        try:
            for x, y in ((-1, 0), (1, 0), (-1, 1.25), (0, 1.25), (1, 1.25), (0, -1.5)):
                col, row = int(((x + shift) / 4 + 0.5) * 96), int((y / 4 + 0.5) * 96)
                if not np.allclose(self.tab.pixel("C", col, row)[:3], pixels[row, col, :3], atol=3e-4, rtol=0):
                    return False
        except hou.OperationFailed:
            return False
        return True

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
                    assert (
                        status["state"] == "closed"
                        and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                self.detach()
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert now - self.started < 600, self.report
            assert now - self.edited_at < (150 if self.index == 0 else 45), self.report
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
            measured = self.f.assert_pixels(entry.session.frame())
            issues = status["source_diagnostics"]
            assert {issue["code"] for issue in issues} == self.f.codes, status
            assert all(issue["source"] and issue["message"] and issue["action"] for issue in issues), issues
            assert len(registry.entries()) == 1 and status["packed_instances"] == 12, status
            assert status["geometry_resources"] == 4 and status["points"] == 3 and status["curves"] == 2, status
            assert status["render_device_ids"] == self.f.target_ids and self.f.authored() == self.authored, status
            if self.f.config.get("animate"):
                assert status["motion_cooks"] > 0 and not status["motion_warnings"], status
            pixels = self.f.pixels(entry.session.frame())
            if self.native:
                if not self.native_matches(pixels):
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            cooks = self.cooks()
            label = self.steps[self.index][0]
            if self.report["events"]:
                first, previous = self.report["events"][0], self.report["events"][-1]
                assert status["renderer_pid"] == first["status"]["renderer_pid"], status
                assert cooks == first["cooks"], (cooks, first["cooks"])
                if (
                    bool(self.f.config.get("animate")) == previous["deformation_mode"]
                    and self.f.isolated_mode == previous["isolated_curve_object"]
                ):
                    assert status["displacement_rebuilds"] == previous["status"]["displacement_rebuilds"], status
                if "camera navigation" in label:
                    assert status["geometry_conversions"] == previous["status"]["geometry_conversions"], (
                        previous,
                        status,
                    )
            self.report["events"].append(
                dict(
                    label=label,
                    status=status,
                    frame_info=info,
                    cooks=cooks,
                    expected_curves=self.f.expected_curves.tolist(),
                    expected_particles=self.f.expected_particles.tolist(),
                    measured=measured,
                    authored_material_unchanged=True,
                    deformation_mode=bool(self.f.config.get("animate")),
                    isolated_curve_object=self.f.isolated_mode,
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
            assert self.f.authored() == self.authored
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

    def detach(self):
        self.heartbeat.stop()
        if self.callback in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.callback)

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
        self.deadline = time.monotonic() + 30


def start():
    global workflow
    workflow = Workflow()


if __name__ == "__main__":
    start()
