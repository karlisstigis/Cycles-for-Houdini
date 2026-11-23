# SPDX-License-Identifier: Apache-2.0
"""Live dense material binding edits, source repairs and queued navigation.

HOM edits actual parameters while one viewer and worker remain active. This
checks image delivery and ownership, not physical mouse-input latency budgets.
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
from stylesheet_storage_fixture import Fixture


class Workflow:
    def __init__(self, fixture_type=Fixture):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_TRANSPORT_VIEWER") == "native"
        self.f = fixture_type(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.key = f"rop:{self.f.rop.sessionId()}"
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.steps = [
            ("Uniform", lambda: None, False, False, False),
            ("Four styles", lambda: self.f.styles(), False, False, False),
            ("Surface edit", lambda: self.f.styles(offset=0.04), False, False, False),
            ("Unsupported export", lambda: self.f.styles(offset=0.04, bad=True), True, False, False),
            ("Export repair", lambda: self.f.styles(offset=0.04), False, False, False),
            ("Surface edit with navigation", self.surface_navigation, False, False, False),
            ("Prototype edit with navigation", self.geometry_navigation, False, False, True),
            ("Prototype repair", lambda: self.f.prototype.parmTuple("t").set((0, 0, 0)), False, False, True),
            ("Remove styles", lambda: self.f.styles("uniform"), False, False, False),
            ("Restore styles", lambda: self.f.styles(), False, False, False),
            ("Internal face binding", self.face_binding, False, False, False),
            ("Repair face binding", lambda: self.f.styles(), False, False, False),
            ("Camera only", lambda: self.f.camera.parm("tx").set(-0.05), False, True, False),
            ("Object only", lambda: self.f.obj.parm("tx").set(-0.05), False, True, False),
            ("Exact-energy palette basis", self.energy_basis, False, True, False),
            ("Equal-energy hue edit", self.energy_hue, False, True, False),
        ]
        self.index = self.generation = self.navigation_pending = self.navigation_events = 0
        self.panel = self.entry = self.session = None
        self.phase, self.busy, self.finished = "mount", False, False
        self.began = self.edited_at = self.heartbeat_at = time.monotonic()
        self.heartbeats = []
        self.step_heartbeat_begin = 0
        self.report = dict(result="running", events=[], viewer="native" if self.native else "cycles", scope=__doc__)
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setInterval(20)
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start()
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def beat(self):
        now = time.monotonic()
        self.heartbeats.append(now - self.heartbeat_at)
        self.heartbeat_at = now

    def navigate(self):
        for index, delay in enumerate((10, 30, 50)):
            self.navigation_pending += 1

            def edit(index=index):
                if self.finished or self.phase == "closing":
                    return
                try:
                    self.f.camera.parm("tx").set(0.02 * (index + 1))
                    self.f.obj.parm("tx").set(0.015 * (index + 1))
                    self.navigation_events += 1
                    self.navigation_pending -= 1
                    self.edited_at = time.monotonic()
                except BaseException:
                    self.close(traceback.format_exc())

            QtCore.QTimer.singleShot(delay, edit)

    def surface_navigation(self):
        self.f.styles(offset=0.07)
        self.navigate()

    def geometry_navigation(self):
        self.f.prototype.parmTuple("t").set((0, 0, 0.1))
        self.navigate()

    def energy_basis(self):
        self.f.colors = ((0.125, 0.25, 0.5), (0.125, 0.5, 0.25), (0.25, 0.125, 0.5), (0.25, 0.5, 0.125))
        self.f.styles()

    def energy_hue(self):
        self.f.colors = tuple((c[2], c[0], c[1]) for c in self.f.colors)
        self.f.styles()

    def face_binding(self):
        self.f.styles("uniform")
        last = int(self.f.prototype.geometry().intrinsicValue("primitivecount")) // 2 - 1
        rule = dict(
            target=dict(group="0", subTarget=dict(group=f"0-{last}")),
            overrides=dict(materialParameters=dict(tint=[0.9, 0.9, 0.2])),
        )
        self.f.obj.parm("shop_materialstylesheet").set(json.dumps(dict(styles=[rule])))

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
                    heartbeat_intervals=dict(
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
            assert time.monotonic() - self.began < 600, (self.phase, self.index)
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
            label, _, warning, retained, cooked = self.steps[self.index]
            frame = entry.session.frame()
            colors = self.f.check(frame, self.f.offset)
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            assert status["packed_instances"] == 12 and len(registry.entries()) == 1, status
            assert status["mesh_variant_resources"] == (2 if label == "Internal face binding" else 1), status
            assert status["mesh_family_members"] == status["geometry_resources"], status
            assert status["mesh_family_resources"] == status["mesh_variant_resources"], status
            assert (
                status["mesh_variant_bindings"] == status["mesh_metric_records"] == status["mesh_variant_resources"]
            ), status
            if label == "Internal face binding":
                assert np.max(np.abs(np.asarray(colors) - (0.9, 0.9, 0.2)), axis=1).min() < 3e-4, colors
            if self.native:
                if not entry.helper_pid:
                    return
                p = frame["planes"]["combined"]
                pixels = np.frombuffer(p["pixels"], np.float32).reshape(self.f.height, self.f.width, p["channels"])
                try:
                    if not np.allclose(self.tab.pixel("C", 72, 72)[:3], pixels[72, 72, :3], atol=3e-4, rtol=0):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert (
                    entry.views == 1 and not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                )
            if self.report["events"]:
                previous = self.report["events"][-1]
                assert status["renderer_pid"] == previous["status"]["renderer_pid"], status
                assert self.f.prototype.cookCount() == previous["prototype_cooks"] + int(cooked), (
                    label,
                    self.f.prototype.cookCount(),
                    previous,
                )
                if retained:
                    assert status["geometry_conversions"] == previous["status"]["geometry_conversions"], status
                if label in ("Exact-energy palette basis", "Equal-energy hue edit"):
                    assert status["mesh_topology_builds"] == previous["status"]["mesh_topology_builds"], status
                    assert status["mesh_triangulated_faces"] == previous["status"]["mesh_triangulated_faces"], status
            self.generation = info["generation"]
            self.report["events"].append(
                dict(
                    label=label,
                    colors=colors,
                    status=status,
                    prototype_cooks=self.f.prototype.cookCount(),
                    navigation_events=self.navigation_events,
                    maximum_heartbeat_seconds=max(self.heartbeats[self.step_heartbeat_begin :], default=0),
                )
            )
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                assert self.navigation_events == 6
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.step_heartbeat_begin = len(self.heartbeats)
            self.steps[self.index][1]()
            self.edited_at = time.monotonic()
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


if __name__ == "__main__":
    workflow = Workflow()
