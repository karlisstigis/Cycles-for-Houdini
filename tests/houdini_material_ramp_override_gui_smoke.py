# SPDX-License-Identifier: Apache-2.0
"""One live viewer: native ramp/curve overrides, local faults and repairs.

Edits are programmatic HOM parameter edits driven by Houdini's UI callback.
The test checks actual displayed pixels; it is not physical-input certification.
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
from material_ramp_override_fixture import Fixture, CURVE_CASES


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_RAMP_OVERRIDE_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], heavy=True)
        self.f.rop.setCurrent(True, clear_all_selected=True)
        self.key = "rop:" + str(self.f.rop.sessionId())
        self.expected = np.asarray((0.33, 0.33, 0.3))
        self.expected_z = 0.15
        self.displacement_baseline = None
        self.height_switch = False
        self.authored = self.f.authored()
        self.steps = [
            ("Native three-stop Color and Alpha override", lambda: None, False, False, False),
            ("Alpha Local Override changes Color times Alpha", lambda: self.alpha(0.6), False, False, False),
            (
                "Missing added Alpha stop retains authored lower count",
                lambda: self.rename_alpha("unknown"),
                True,
                False,
                False,
            ),
            (
                "Cached ramp warning survives camera navigation",
                lambda: self.f.camera.parm("tx").set(0.2),
                True,
                False,
                True,
            ),
            (
                "Alpha stop repair restores the whole Alpha ramp",
                lambda: self.rename_alpha("palette_alpha3value"),
                False,
                False,
                False,
            ),
            (
                "Object navigation retains ramp and independent displacement",
                lambda: self.f.obj.parm("tx").set(0.1),
                False,
                False,
                True,
            ),
            ("Native vector curve reference through native stop dictionary", self.vector_curve, False, True, False),
            (
                "Missing curve stop field falls back to its authored stop",
                lambda: self.rename_curve("unknown"),
                True,
                False,
                False,
            ),
            (
                "Cached curve warning survives camera navigation",
                lambda: self.f.camera.parm("tx").set(0.4),
                True,
                False,
                True,
            ),
            (
                "Curve stop repair restores native reference",
                lambda: self.rename_curve("vector_curves_x1pos"),
                False,
                False,
                False,
            ),
            ("Float Curve (Float Ramp replacement) count and stop dictionary", self.scalar_ramp, False, True, False),
            ("Float Curve stop edit", lambda: self.scalar_value(0.2), False, False, False),
            ("Float Curve stop repair", lambda: self.scalar_value(0.4), False, False, False),
            ("Restore whole RGBA ramp", self.restore_rgba, False, True, False),
            (
                "Camera navigation retains restored RGBA controls",
                lambda: self.f.camera.parm("tx").set(0.1),
                False,
                False,
                True,
            ),
            ("Independent Alpha override drives true height", self.alpha_height, False, True, False),
            (
                "Authored RGB fault retains independent Alpha and true height",
                lambda: self.authored_rgb_fault(True),
                "material.ramp_unavailable",
                True,
                False,
            ),
            (
                "Authored RGB repair restores color and clears warning",
                lambda: self.authored_rgb_fault(False),
                False,
                True,
                False,
            ),
            (
                "Promoted curve end stop value override drives independent true height",
                self.curve_height,
                False,
                True,
                False,
            ),
            (
                "Authored curve position fault retains valid end value and independent height",
                lambda: self.authored_curve_fault(True),
                "material.ramp_unavailable",
                True,
                False,
            ),
            (
                "Authored curve position repair restores color and clears warning",
                lambda: self.authored_curve_fault(False),
                False,
                True,
                False,
            ),
            ("Supported relative channel arithmetic uses the owned override", self.lazy_expression, False, True, False),
            (
                "First-read relative Python dependency omits only its override",
                lambda: self.lazy_expression_python(True),
                True,
                True,
                False,
            ),
            (
                "Relative Hscript repair restores owned values and clears warning",
                lambda: self.lazy_expression_python(False),
                False,
                True,
                False,
            ),
        ]
        self.index = self.generation = 0
        self.entry = self.panel = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.started = self.edited_at = time.monotonic()
        self.saved_at = self.heartbeats = 0
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            scope=__doc__,
            physical_input_certification=False,
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

    def alpha(self, value):
        self.f.local("palette_alpha3value", "fval").set(value)
        self.alpha_value = value
        self.expected = np.asarray((0.55, 0.55, 0.5)) * ((0.8 + value) * 0.5)

    def rename_alpha(self, name):
        # Independent Alpha stops: a missing added Alpha stop drops only the
        # Alpha count override (Alpha .8 beyond stop 2); Color keeps 3 stops.
        self.f.local("palette_alpha3value", "name").set(name)
        self.expected = np.asarray((0.55, 0.55, 0.5)) * (0.8 if name == "unknown" else (0.8 + self.alpha_value) * 0.5)

    def vector_curve(self):
        self.curve_expected = self.f.curve_reference(CURVE_CASES["vector_mixed"])
        self.expected = np.asarray(self.curve_expected)

    def rename_curve(self, name):
        self.f.local("vector_curves_x1pos", "name").set(name)
        # The authored promoted X ramp's first stop is also at position 0, so
        # the image is unchanged; only the ignored field warns.
        self.expected = np.asarray(self.curve_expected).copy()

    def scalar_ramp(self):
        self.expected = self.f.scalar_ramp()

    def scalar_value(self, value):
        self.f.local("profile3value", "fval").set(value)
        self.expected = np.repeat((0.8 + value) * 0.5, 3)

    def restore_rgba(self):
        self.expected = self.f.simple_rgba()

    def alpha_height(self):
        self.expected, self.expected_z = self.f.alpha_height_case()
        self.height_switch = True

    def authored_rgb_fault(self, broken):
        self.expected = np.asarray(self.f.authored_rgb_fault(broken))

    def curve_height(self):
        self.expected, self.expected_z = self.f.curve_height_case()
        self.height_switch = True

    def authored_curve_fault(self, broken):
        self.expected = np.asarray(self.f.authored_curve_fault(broken))

    def lazy_expression(self):
        self.expected, self.expected_z = self.f.lazy_expression_case()
        self.height_switch = True

    def lazy_expression_python(self, enabled):
        self.expected = np.asarray(self.f.lazy_expression_python(enabled))

    def cooks(self):
        heavy = self.f.heavy.renderNode()
        return dict(
            receiver=self.f.sop.cookCount(),
            assigned=self.f.assigned.cookCount(),
            heavy=heavy.cookCount(),
            prototype=heavy.inputs()[0].cookCount(),
        )

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
        metadata = (getattr(self.panel.canvas, "frame_metadata", None) or {}) if self.panel else {}
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
            displayed=dict(metadata),
        )
        self.save()

    def detach(self):
        self.heartbeat.stop()
        if self.callback in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.callback)

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
            label, _, warnings, _, fast = self.steps[self.index]
            frame = entry.session.frame()
            self.f.assert_pixels(frame, self.expected, z=self.expected_z)
            issues = status["source_diagnostics"]
            warning_code = warnings if isinstance(warnings, str) else "material.override_ignored"
            assert {item["code"] for item in issues} == ({warning_code} if warnings else set()), issues
            if warnings:
                assert all(
                    item["source"] == self.f.material.path() and item["action"] and item["message"] for item in issues
                ), issues
            assert (
                len(registry.entries()) == 1 and status["packed_instances"] == 12 and status["geometry_resources"] == 2
            ), status
            assert status["object_resources"] == 13 and self.f.authored() == self.authored, status
            pixels = self.f.pixels(frame)
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (64, 64):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x, :3], atol=3e-4, rtol=0)
                        for x, y in ((31, 31), (32, 31), (31, 32), (32, 32))
                    ):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            cooks = self.cooks()
            if self.report["events"]:
                first, previous = self.report["events"][0], self.report["events"][-1]
                assert status["renderer_pid"] == first["status"]["renderer_pid"], status
                assert status["geometry_conversions"] == first["status"]["geometry_conversions"], status
                if self.height_switch:
                    assert status["displacement_rebuilds"] > self.displacement_baseline, status
                else:
                    assert status["displacement_rebuilds"] == self.displacement_baseline, status
                assert status["subdivision_tessellations"] == first["status"]["subdivision_tessellations"], status
                assert all(cooks[name] == first["cooks"][name] for name in ("receiver", "heavy", "prototype")), cooks
                if fast:
                    assert cooks == previous["cooks"], (cooks, previous["cooks"])
            self.displacement_baseline = status["displacement_rebuilds"]
            self.report["events"].append(
                dict(
                    label=label,
                    status=status,
                    frame_info=info,
                    cooks=cooks,
                    expected_rgb=self.expected.tolist(),
                    measured_rgb=pixels[31:33, 31:33, :3].mean((0, 1)).tolist(),
                    expected_z=self.expected_z,
                    intentional_height_change=self.height_switch,
                    z_range=[
                        float(self.f.pixels(frame, "position")[31:33, 31:33, 2].min()),
                        float(self.f.pixels(frame, "position")[31:33, 31:33, 2].max()),
                    ],
                    authored_material_unchanged=True,
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
            _, action, _, changes_material, _ = self.steps[self.index]
            self.height_switch = False
            action()
            if changes_material:
                self.authored = self.f.authored()
            else:
                assert self.f.authored() == self.authored, "Native SOP override edited the authored material"
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
        self.deadline = time.monotonic() + 30


def start():
    global workflow
    workflow = Workflow()


if __name__ == "__main__":
    start()
