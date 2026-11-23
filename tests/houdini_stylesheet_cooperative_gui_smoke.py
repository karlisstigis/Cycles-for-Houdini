# SPDX-License-Identifier: Apache-2.0
"""Live stylesheet continuations: navigation, file repairs, cancellation and source removal.

One real viewer/worker receives current HOM parameter captures. Automatic scene
submission is disabled so edits can be applied at proven continuation boundaries.
Image delivery remains the normal viewer path; pixel masks are independent.
"""

import pass_fixtures as pass_fixture

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
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_COOPERATIVE_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.material_motion = os.environ.get("CYCLES_HOUDINI_COOPERATIVE_MOTION") == "1"
        if self.material_motion:
            self.f.enable_material_motion(general_bindings=True, displacement=True)
        pass_fixture.set_parameters(self.f.rop, dict(pass_position=1, pass_depth=1))
        self.file = self.f.dest / "cooperative-import.json"
        self.control = pass_fixture.parm(self.f.obj, "shop_materialstylesheet")
        self.key = "rop:" + str(self.f.rop.sessionId())
        self.panel = self.entry = self.session = None
        self.target = 0
        self.worker = None
        self.cooks = None
        self.index = 0
        self.hook = 0
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.last_beat = time.monotonic()
        self.beats = []
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            material_motion=self.material_motion,
            scope=__doc__,
            events=[],
            pending_edits=[],
        )
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(False)
            self.tab.startRender()
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.timer = QtCore.QTimer()
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def beat(self):
        now = time.monotonic()
        self.beats.append(now - self.last_beat)
        self.last_beat = now

    def rule(self, color):
        last = int(self.f.prototype.geometry().intrinsicValue("primitivecount")) // 2 - 1
        return json.dumps(
            dict(
                styles=[
                    dict(
                        target=dict(group="0", subTarget=dict(group=f"0-{last}")),
                        overrides=dict(materialParameters=dict(tint=color)),
                    )
                ]
            )
        )

    def submit(self, color):
        self.color = color
        self.hook = 0
        self.file.write_text(self.rule(color))
        self.control.set(json.dumps(dict(importFiles=[self.file.resolve().as_posix()])))
        self.target = self.session.apply(self.f.update())
        self.phase = "pending"

    def edited(self, label, active):
        self.report["pending_edits"].append(
            dict(label=label, yields=active["preparation_yields"], stage=active["stage"])
        )
        self.save()

    def pending(self):
        active = self.session._active_capture
        if not active or active["stage"] != "styles" or active["preparation_yields"] < self.hook + 1:
            return
        if self.index in (1, 2):
            if self.hook == 0:
                pass_fixture.parm(self.f.camera, "tx").set(0.08 * self.index)
                pass_fixture.parm(self.f.obj, "tx").set(0.025 * self.index)
                if self.index == 1:
                    self.file.write_text("{broken")
                else:
                    self.file.unlink()
                self.target = self.session.apply(self.f.update())
                self.edited("Break import and navigate" if self.index == 1 else "Delete import and navigate", active)
            elif self.hook == 1:
                self.color = (0.9, 0.9, 0.35 if self.index == 1 else 0.5)
                self.file.write_text(self.rule(self.color))
                self.target = self.session.apply(self.f.update())
                self.edited("Repair imported source" if self.index == 1 else "Recreate imported source", active)
            else:
                return
            self.hook += 1
        elif self.index == 3:
            self.edited("Cancel during continuation", active)
            self.session.cancel()
            self.phase = "cancel"
            self.hook += 1
        elif self.index == 5:
            self.edited("Remove source during continuation", active)
            self.target = self.session.apply(dict(geometry=[]))
            self.phase = "empty"
            self.hook += 1

    def image(self, frame):
        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(self.f.height, self.f.width, p["channels"])

        rgb = plane("combined")[:, :, :3]
        assert np.isfinite(rgb).all()
        if self.phase == "empty":
            assert np.count_nonzero(rgb) == 0
            return
        position = plane("position")
        depth = plane("depth")[:, :, 0]
        for number, point in enumerate(self.f.targets.geometry().points()):
            center = point.position() * self.f.obj.worldTransform()
            mask = (
                (depth > 0)
                & (depth < 100)
                & (np.abs(position[:, :, 0] - center[0]) < 0.8)
                & (np.abs(position[:, :, 1] - center[1]) < 0.7)
            )
            assert mask.sum() > 20, (number, int(mask.sum()))
            if self.material_motion:
                normal = self.f.prototype.geometry().prim(0).normal()
                np.testing.assert_allclose(position[:, :, 2][mask], center[2] + normal[2] * 0.02, atol=3e-4, rtol=0)
            if number == 0 and self.index:
                g = self.f.prototype.geometry()
                half = int(g.intrinsicValue("primitivecount")) // 2
                center_of = lambda prim: np.mean([tuple(v.point().position()) for v in prim.vertices()], axis=0)
                first, opposite = center_of(g.prim(0)), center_of(g.prim(half))
                axis = int(np.argmax(np.abs(first - opposite)))
                boundary = g.boundingBox().center()[axis]
                local = position[:, :, axis] - center[axis] - boundary
                chosen = mask & (local < -0.03 if first[axis] < boundary else local > 0.03)
                plain = mask & (local > 0.03 if first[axis] < boundary else local < -0.03)
                for selected, color in ((chosen, self.color), (plain, self.f.colors[0])):
                    assert selected.sum() > 20
                    np.testing.assert_allclose(
                        rgb[selected], np.broadcast_to(color, (selected.sum(), 3)), atol=3e-4, rtol=0
                    )
            else:
                np.testing.assert_allclose(
                    rgb[mask], np.broadcast_to(self.f.colors[0], (mask.sum(), 3)), atol=3e-4, rtol=0
                )

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
                        and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0
                    ), status
                self.timer.stop()
                self.report.update(
                    result="failed" if self.error else "passed",
                    error=self.error,
                    closed=status,
                    heartbeat=dict(
                        count=len(self.beats),
                        maximum=max(self.beats, default=0),
                        p95=float(np.percentile(self.beats, 95)) if self.beats else 0,
                    ),
                )
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 420, (self.phase, self.index)
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    self.panel.auto.setChecked(False)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "initial"
            self.entry = registry.get(self.key)
            if self.entry is None:
                return
            self.session = self.entry.session
            status = self.session.status()
            assert status["state"] != "failed" and not self.entry.error, (status, self.entry.error)
            if self.worker is not None:
                assert status["renderer_pid"] == self.worker, status
                assert self.f.prototype.cookCount() == self.cooks, (self.cooks, self.f.prototype.cookCount())
            if self.phase == "pending":
                self.pending()
            if self.phase == "cancel":
                if (
                    status.get("capture_pending")
                    or status.get("pending_update")
                    or status["backend_state"] != "cancelled"
                ):
                    return
                self.report["events"].append(dict(label="Cancelled continuation retired", status=status))
                self.save()
                self.index = 4
                self.submit((0.9, 0.9, 0.65))
                return
            info = self.session.frame_info()
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            if self.target and info["generation"] != self.target:
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            frame = self.session.frame()
            self.image(frame)
            assert not status["source_diagnostics"], status["source_diagnostics"]
            if self.native:
                if not self.entry.helper_pid:
                    return
                p = frame["planes"]["combined"]
                pixels = np.frombuffer(p["pixels"], np.float32).reshape(self.f.height, self.f.width, p["channels"])
                try:
                    if not np.allclose(self.tab.pixel("C", 72, 72)[:3], pixels[72, 72, :3], atol=3e-4, rtol=0):
                        return
                except hou.OperationFailed:
                    return
            assert len(registry.entries()) == 1
            if self.index in (1, 2):
                assert self.hook == 2, (self.index, self.hook)
            if self.index == 5:
                assert self.hook == 1 and status["packed_instances"] == 0, status
            self.report["events"].append(
                dict(index=self.index, status=status, capture=self.session.diagnostics()["capture"])
            )
            self.save()
            if self.index == 0:
                self.worker = status["renderer_pid"]
                self.cooks = self.f.prototype.cookCount()
            self.index += 1
            if self.index == 6:
                assert len(self.report["pending_edits"]) == 6, self.report["pending_edits"]
                self.close()
                return
            self.submit((0.9, 0.9, 0.1 * self.index))
        except BaseException:
            self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        if self.phase == "closing":
            self.finished = True
            self.timer.stop()
            self.report.update(result="failed", error=error)
            self.save()
            hou.ui.removeEventLoopCallback(self.callback)
            hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            return
        self.error = error
        self.phase = "closing"
        self.deadline = time.monotonic() + 20
        if self.native:
            self.tab.killRender()
        elif self.panel:
            self.panel.dispose()
        registry.close_all()


if __name__ == "__main__":
    workflow = Workflow()
