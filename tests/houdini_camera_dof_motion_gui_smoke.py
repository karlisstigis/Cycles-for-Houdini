# SPDX-License-Identifier: Apache-2.0
"""One actual viewer: camera shutter optics, local recovery and retained geometry.

Native parameter edits and real viewer callbacks are covered. This does not
claim physical mouse/compositor latency or replace the separate response gate.
"""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from camera_dof_motion_fixture import Fixture, plane


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_CAMERA_DOF_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.add_heavy()
        self.steps = []

        def add(label, action, retain=True):
            self.steps.append((label, action, retain))

        add("Continuous focus and independent motion", lambda: None, False)
        add("Same nominal focus with changed shutter samples", lambda: f.camera.parm("test_dof_amplitude").set(0.5))
        add("Exact focus repair", lambda: f.camera.parm("test_dof_amplitude").set(1))
        for case in (
            "offframe_bad",
            "focus",
            "nominal_bad",
            "focus",
            "disabled",
            "focus",
            "ratio_bad",
            "focus",
            "aperture",
            "ratio",
            "rotation",
            "blade_hold",
            "focus",
        ):
            add("Live lens case " + case, lambda case=case: f.set_case(case))
        add("Offset exposure retains nominal lens", lambda: f.set_case("focus", offset=1), False)
        add("Offset shutter-only repair", lambda: f.camera.parm("test_dof_amplitude").set(0.5))
        add("Rolling exposure consumes sampled lens time", lambda: f.set_case("rolling"), False)
        add("Orthographic focus motion", lambda: f.set_case("ortho"), False)
        self.index = self.generation = 0
        self.phase = "mount"
        self.panel = self.entry = self.session = None
        self.busy = self.finished = False
        self.began = self.edited_at = time.monotonic()
        self.report = dict(result="running", viewer="native" if self.native else "cycles", scope=__doc__, events=[])
        self.callback = self.tick
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
            self.tab.startRender()
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        hou.ui.addEventLoopCallback(self.callback)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

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
                        and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                if self.callback in hou.ui.eventLoopCallbacks():
                    hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 600, (self.phase, self.index)
            assert time.monotonic() - self.edited_at < 90, (self.phase, self.index, self.report.get("waiting"))
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            self.entry = entry = registry.get("rop:" + str(self.f.rop.sessionId()))
            if entry is None:
                return
            status, info = entry.session.status(), entry.session.frame_info()
            self.report["waiting"] = dict(
                index=self.index,
                label=self.steps[self.index][0],
                status=status,
                change_serial=entry.change_serial,
                applied_serial=entry.applied_serial,
                requested_serial=entry.requested_serial,
            )
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or time.monotonic() - self.edited_at < 0.3
                or status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or info["generation"] <= self.generation
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            label, _, retain = self.steps[self.index]
            frame = entry.session.frame()
            captured = self.f.capture_info(entry.session)
            cooks = (self.f.sop.cookCount(), self.f.prototype.cookCount())
            event = dict(
                label=label,
                case=self.f.case,
                status=status,
                captured=captured,
                cooks=cooks,
                generation=info["generation"],
                sequence=info["sequence"],
            )
            self.report["attempted"] = event
            self.save()
            event["image"] = self.f.inspect(frame)
            assert {issue["code"] for issue in status["source_diagnostics"]} == self.f.expected_codes() and not status[
                "motion_warnings"
            ], event
            assert captured["camera_dof_samples"] == self.f.expected_samples(), event
            assert captured["camera_motion_samples"] == 3 and captured["mesh_motion_samples"] == 2, event
            assert (
                status["geometry_resources"] == 2
                and status["object_resources"] == 13
                and status["packed_instances"] == 12
            ), event
            assert len(registry.entries()) == 1, event
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (self.f.width, self.f.height):
                    return
                pixels = plane(frame)
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x, :3], atol=3e-4, rtol=0)
                        for x, y in ((64, 48), (70, 48), (80, 48), (95, 48))
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            if self.report["events"]:
                first, previous = self.report["events"][0], self.report["events"][-1]
                assert status["renderer_pid"] == first["status"]["renderer_pid"] and cooks[1] == first["cooks"][1], (
                    event
                )
                if retain:
                    assert cooks == tuple(previous["cooks"]), event
                    assert all(
                        status[key] == previous["status"][key]
                        for key in ("geometry_conversions", "extractions", "subdivision_tessellations")
                    ), event
                    if status["render_revision"] != previous["status"]["render_revision"]:
                        assert (
                            not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys()
                        ), event
            self.report["events"].append(event)
            self.report.pop("attempted", None)
            self.save()
            self.generation = info["generation"]
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.steps[self.index][1]()
            self.edited_at = time.monotonic()
        except BaseException:
            if self.phase == "closing":
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                if self.callback in hou.ui.eventLoopCallbacks():
                    hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        self.error = error
        if error:
            self.report["last_image_check"] = getattr(self.f, "last_inspection", None)
        self.session = self.entry.session if self.entry else None
        if self.native:
            self.tab.killRender()
        elif self.panel:
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
            self.panel.dispose()
        registry.close_all()
        self.phase = "closing"
        self.deadline = time.monotonic() + 30


if __name__ == "__main__":
    workflow = Workflow()
