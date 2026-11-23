# SPDX-License-Identifier: Apache-2.0
"""One live viewer: nonlinear temporal packed-field edits and local repairs.

HOM parameter changes and actual viewer callbacks are exercised with a retained
million-triangle prototype. This is not physical-input latency certification.
"""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import object_properties, registry
from sampled_instance_attribute_fixture import Fixture, plane


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_SAMPLED_INSTANCE_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.stable_nominal(True)
        f.add_heavy()
        object_properties.install(f.obj)
        self.steps = []

        def add(label, action, retain=False, fast=False):
            self.steps.append((label, action, retain, fast))

        add("Nonlinear requested-frame-equal temporal field", lambda: None)
        add("Off-frame-only amplitude changes", lambda: f.configure(amplitude=0.55), True)
        add("Non-finite off-frame tuple", lambda: f.configure(fault=1), True)
        add("Off-frame tuple repair", f.configure, True)
        add("Missing off-frame field", lambda: f.configure(fault=2), True)
        add("Cached warning survives camera navigation", lambda: f.camera.parm("tx").set(0.04), True, True)
        add("Off-frame missing field repair", f.configure, True)
        add("Wrong off-frame tuple size", lambda: f.configure(fault=3), True)
        add("Off-frame schema repair", f.configure, True)
        add("Shutter before requested frame", lambda: f.rop.parm("shutteroffset").set(-1), True)
        add("Shutter after requested frame", lambda: f.rop.parm("shutteroffset").set(1), True)
        add("Integer field with centred shutter", self.integer)
        add("Integer temporal amplitude edit", lambda: f.configure("counter", amplitude=0.55), True)
        add("Vector field", lambda: f.configure("vector_field"))
        add("Vector off-frame failure", lambda: f.configure("vector_field", fault=1), True)
        add("Vector off-frame repair", lambda: f.configure("vector_field"), True)
        add("Color Attribute Cd on packed points", lambda: f.configure("Cd", owner="point", color_attribute=True))
        add("Cd off-frame missing", lambda: f.configure("Cd", owner="point", fault=2, color_attribute=True), True)
        add("Cd off-frame repair", lambda: f.configure("Cd", owner="point", color_attribute=True), True)
        add("Nearest nested owner", lambda: f.configure(nested=True))
        add("Valid sampled ancestor", lambda: f.configure(ancestor=True), True)
        add("Ordinary sampled owner", f.configure, True)
        add("Object navigation retains owned samples", lambda: f.obj.parm("tx").set(0.05), True, True)
        add("Camera navigation retains owned samples", lambda: f.camera.parm("tx").set(0.08), True, True)
        add("Per-object blur disabled", lambda: f.obj.parm("cycles_motion_blur").set("off"))
        add("Per-object blur restored", lambda: f.obj.parm("cycles_motion_blur").set("inherit"))
        add("Independent geometry and transform motion", self.deformation)
        add("Independent motion survives broken field", lambda: f.configure(fault=1))
        add("Independent motion and field repair", f.configure)
        add("Return to static prototype", self.static)
        add("Velocity mode with missing v", self.velocity_mode)
        add(
            "Valid packed velocity keeps temporal fields",
            lambda: f.configure(owner="point", duplicates=True, velocity=True),
            True,
        )
        add(
            "Valid packed velocity with broken shutter field",
            lambda: f.configure(owner="point", duplicates=True, velocity=True, fault=1),
            True,
        )
        add(
            "Valid packed velocity shutter repair",
            lambda: f.configure(owner="point", duplicates=True, velocity=True),
            True,
        )
        add("Removing v retains temporal fields", lambda: f.configure(owner="point", duplicates=True), True)
        add("Restore transform mode and ordinary fields", self.restore_fields)
        for kind in (1, 2):
            add("Live point/hair prototype " + str(kind), lambda kind=kind: f.configure("vector_field", kind=kind))
            add(
                "Live point/hair field hold " + str(kind),
                lambda kind=kind: f.configure("vector_field", kind=kind, fault=1),
                True,
            )
            add(
                "Live point/hair field repair " + str(kind),
                lambda kind=kind: f.configure("vector_field", kind=kind),
                True,
            )
        self.index = self.generation = 0
        self.panel = self.entry = self.session = None
        self.preview_session = self.original_preview = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited_at = time.monotonic()
        self.report = dict(
            result="running",
            scope=__doc__,
            viewer="native" if self.native else "cycles",
            events=[],
            pose_preview_calls=[],
        )
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

    def integer(self):
        self.f.stable_nominal(False)
        self.f.rop.parm("shutteroffset").set(0)
        self.f.configure("counter")

    def deformation(self):
        self.f.rop.parm("geometry_motion").set("deformation")
        self.f.prototype_fields.parm("snippet").set("@P.z=.01*@Frame;")
        self.f.obj.parm("tx").setExpression(".05+$T*1.2", hou.exprLanguage.Hscript)
        self.f.camera.parm("tx").setExpression(".08+$T*1.4", hou.exprLanguage.Hscript)

    def static(self):
        self.f.prototype_fields.parm("snippet").set("")
        for node in (self.f.obj, self.f.camera):
            node.parm("tx").deleteAllKeyframes()
            node.parm("tx").set(0)
        self.f.rop.parm("geometry_motion").set("transform")

    def velocity_mode(self):
        self.f.rop.parm("geometry_motion").set("velocity")
        self.f.configure(owner="point", duplicates=True)

    def restore_fields(self):
        self.static()
        self.f.configure()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def observe_pose_previews(self, session):
        if self.preview_session is session:
            return
        assert self.preview_session is None, "Viewer replaced its persistent session"
        self.preview_session, self.original_preview = session, session.preview_object_poses
        original = self.original_preview

        def observed(update, at_time=None):
            # Observe the real viewer/registry call without creating work,
            # changing scheduling or asserting inside a renderer callback.
            note = dict(
                index=self.index,
                label=self.steps[self.index][0] if self.index < len(self.steps) else "closing",
                cooks_before=(
                    self.f.fields.cookCount(),
                    self.f.output.cookCount(),
                    self.f.nominal_control.cookCount(),
                    self.f.heavy.cookCount(),
                ),
            )
            began = time.monotonic()
            try:
                result = original(update, at_time=at_time)
                note["submission"] = result
                return result
            except BaseException:
                note["error"] = traceback.format_exc()
                raise
            finally:
                note["cooks_after"] = (
                    self.f.fields.cookCount(),
                    self.f.output.cookCount(),
                    self.f.nominal_control.cookCount(),
                    self.f.heavy.cookCount(),
                )
                note["acquisition_call_ms"] = (time.monotonic() - began) * 1000
                self.report["pose_preview_calls"].append(note)

        session.preview_object_poses = observed

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
            assert time.monotonic() - self.began < 900, (self.phase, self.index)
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
            self.observe_pose_previews(entry.session)
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
            label, _, retain, fast = self.steps[self.index]
            frame = entry.session.frame()
            captured = self.f.capture_info(entry.session)
            cooks = (
                self.f.fields.cookCount(),
                self.f.output.cookCount(),
                self.f.nominal_control.cookCount(),
                self.f.heavy.cookCount(),
            )
            event = dict(
                label=label,
                status=status,
                captured=captured,
                shutter_reference=self.f.shutter_reference(),
                cooks_before_inspection=cooks,
            )
            if not fast:
                event["prototype"] = self.f.prototype_ownership()
            if self.index < 11:
                event["nominal_ids"] = self.f.nominal_ids()
            self.report["attempted"] = event
            self.save()
            event["images"] = self.f.inspect(frame)
            event["cooks"] = (
                self.f.fields.cookCount(),
                self.f.output.cookCount(),
                self.f.nominal_control.cookCount(),
                self.f.heavy.cookCount(),
            )
            expected = self.f.expected_incomplete()
            issues = [
                issue
                for issue in status["source_diagnostics"]
                if issue["code"] == "geometry.shutter_instance_attribute"
            ]
            assert bool(issues) == bool(expected) and captured["incomplete_instance_attribute_fields"] == expected, (
                event
            )
            assert not [
                issue
                for issue in status["source_diagnostics"]
                if issue["code"] != "geometry.shutter_instance_attribute"
            ], event
            assert (
                status["geometry_resources"] == 2 and status["object_resources"] == 16 and len(registry.entries()) == 1
            ), status
            assert captured["instance_attribute_fields"] == 4, captured
            # Velocity holds instance fields at the requested frame (task M1).
            disabled = self.f.obj.evalParm("cycles_motion_blur") == "off" or self.f.velocity_mode()
            assert captured["sampled_instance_attribute_fields"] == (0 if disabled else 4 - expected), captured
            if "Independent" in label:
                assert (
                    captured["mesh_motion_samples"] > 0
                    and captured["camera_motion_samples"] == 3
                    and captured["object_motion_samples"] >= 12
                ), captured
            if label.startswith("Valid packed velocity"):
                assert captured["object_motion_samples"] >= 12, captured
            if label in ("Velocity mode with missing v", "Removing v retains temporal fields"):
                assert captured["object_motion_samples"] == 0, captured
            if label.startswith("Live point/hair"):
                assert captured["points"] == int(self.f.kind == 1) and captured["curves"] == int(self.f.kind == 2), (
                    captured
                )
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (self.f.width, self.f.height):
                    return
                pixels = plane(frame, "combined")
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x, :3], atol=3e-4, rtol=0)
                        for x, y in ((43, 25), (118, 25), (43, 75), (118, 75))
                    ):
                        return
                except hou.OperationFailed:
                    return
            if self.report["events"]:
                first, previous = self.report["events"][0], self.report["events"][-1]
                assert status["renderer_pid"] == first["status"]["renderer_pid"] and cooks[3] == first["cooks"][3], (
                    event
                )
                if retain:
                    assert status["geometry_conversions"] == previous["status"]["geometry_conversions"], event
                    if label.startswith("Live point/hair"):
                        assert event["prototype"] == previous["prototype"], event
                if fast:
                    if label.startswith("Object navigation"):
                        calls = [call for call in self.report["pose_preview_calls"] if call["index"] == self.index]
                        submissions = [call["submission"] for call in calls if call.get("submission") is not None]
                        event["pose_preview_calls"] = calls
                        event["authoritative_field_cook_delta"] = [
                            current - prior for current, prior in zip(cooks[:3], previous["cooks"][:3])
                        ]
                        # A fast authoritative SOHO update can consume this edit
                        # before the best-effort preview tick. Observe whichever
                        # production route wins, without injecting a preview.
                        event["navigation_path"] = (
                            "pose_preview_then_authoritative" if submissions else "authoritative_coalesced"
                        )
                        event["navigation_acceptance"] = dict(
                            change_serial=entry.change_serial,
                            applied_serial=entry.applied_serial,
                            requested_serial=entry.requested_serial,
                            pending_update=bool(status.get("pending_update")),
                            capture_pending=bool(status.get("capture_pending")),
                            generation=info["generation"],
                            sequence=info["sequence"],
                            requested_scene_submission=status["requested_scene_submission"],
                            committed_scene_submission=status["committed_scene_submission"],
                        )
                        assert all(
                            not call.get("error") and call["cooks_before"] == call["cooks_after"] for call in calls
                        ), event
                        assert all(
                            0 < submission <= status["requested_scene_submission"] for submission in submissions
                        ), event
                        assert entry.applied_serial == entry.change_serial and not entry.requested_serial, event
                        assert not status.get("pending_update") and not status.get("capture_pending"), event
                        assert status["committed_scene_submission"] == status["requested_scene_submission"] > 0, event
                        assert info["complete"] and info["generation"] == status["generation"] > self.generation, event
                    else:
                        assert cooks[:3] == tuple(previous["cooks"][:3]), event
                if self.index < 11:
                    assert event["nominal_ids"] == first["nominal_ids"], event
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
        if self.preview_session is not None:
            self.preview_session.preview_object_poses = self.original_preview
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
