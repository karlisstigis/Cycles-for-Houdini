# SPDX-License-Identifier: Apache-2.0
"""One viewer: local fallback, file repair, paused edits, Stop and Render."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from transaction_scene_fixture import Fixture

_workflow = None


def start(kind):
    # runpy discards the thin entry script's namespace. PySide keeps weak
    # references to bound slots, so the imported module must own this driver.
    global _workflow
    os.environ["CYCLES_HOUDINI_TRANSACTION_VIEWER"] = kind
    _workflow = Workflow()
    return _workflow


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.kind = os.environ["CYCLES_HOUDINI_TRANSACTION_VIEWER"]
        self.report = dict(
            result="running",
            viewer=self.kind,
            events=[],
            scope=__doc__ + " Scripted HOM edits and real viewer controls; no timing certificate.",
        )
        self.save()
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], heavy=True)
        self.f.rop.setParms(dict(samples=1048576, ipr_samples=1048576))
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.panel = self.viewer = self.session = self.retiring = None
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(
            hou.paneTabType.PythonPanel if self.kind == "cycles" else hou.paneTabType.IPRViewer, size=(850, 600)
        )
        if self.kind == "cycles":
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        else:
            self.viewer = self.tab
            self.viewer.setRopNode(self.f.rop)
            self.viewer.setPreview(True)
            self.viewer.setAutoUpdate(True)
            self.viewer.setDelay(0.02)
            self.viewer.startRender()
        self.started = time.monotonic()
        self.stage = "mount" if self.kind == "cycles" else "initial"
        self.busy = self.finished = False
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(25)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def save(self):
        self.report["phase"] = getattr(self, "stage", "construct")
        self.path.write_text(json.dumps(self.report, indent=2))

    def record(self, label, status):
        self.last_picture = (self.session, status["generation"])
        self.report["events"].append(
            dict(
                name=label,
                status=status,
                native_helper=self.entry.helper_pid if self.entry else 0,
                native_diagnostic=self.entry.error if self.entry else "",
            )
        )
        self.save()

    def picture(self, status, color, background, warning):
        if (
            status.get("capture_pending")
            or status.get("pending_update")
            or status["state"] in ("idle", "failed", "closed", "cancelled")
        ):
            return False
        assert not status["error"] and not status.get("update_rejected"), status
        previous = getattr(self, "last_picture", None)
        if previous and previous[0] is self.session and status["generation"] <= previous[1]:
            return False
        frame = self.session.frame_info()
        if not frame or frame["generation"] != status["generation"] or not frame.get("current_scene"):
            return False
        if bool(status["source_diagnostics"]) != warning:
            return False
        assert status["geometry_resources"] == status["object_resources"] == 1, status
        if self.kind == "cycles":
            if self.panel.canvas.image.isNull():
                return False
            if self.panel.canvas.frame_metadata.get("render_revision") != frame.get("render_revision"):
                return False
            # Interactive previews can carry half-float color values. Keep the
            # ordered full-precision image checks stricter in the shared fixture.
            self.f.check(self.session.frame(), color, background, atol=5e-4)
        else:
            if not self.entry.helper_pid or self.viewer.imageResolution() != (self.f.width, self.f.height):
                return False
            center = self.viewer.pixel("C", self.f.width // 2, self.f.height // 2)[:3]
            corner = self.viewer.pixel("C", 2, 2)[:3]
            if (
                max(abs(a - b) for a, b in zip(center, color)) > 2e-4
                or max(abs(a - b) for a, b in zip(corner, background)) > 2e-4
            ):
                return False
        return True

    def pause(self, enabled):
        if self.kind == "cycles":
            QtTest.QTest.mouseClick(self.panel.pause_button, QtCore.Qt.LeftButton)
        elif enabled:
            self.viewer.pauseRender()
        else:
            self.viewer.resumeRender()

    def stop(self):
        if self.kind == "cycles":
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
        else:
            self.viewer.killRender()

    def render(self):
        if self.kind == "cycles":
            QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        else:
            self.viewer.startRender()

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 220, self.stage
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                self.panel.auto.setChecked(True)
                self.render()
                self.stage = "initial"
                return
            if self.retiring is not None:
                status = self.retiring.status()
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                self.report["closed"] = status
                self.finish()
                return
            self.entry = registry.get(self.key)
            if self.entry is None:
                if self.stage != "stopped":
                    return
                status = self.session.status()
            else:
                self.session = self.entry.session
                status = self.session.status()
            self.report["current"] = dict(stage=self.stage, status=status)
            if self.stage == "initial":
                if not self.picture(status, self.f.color, self.f.background, False):
                    return
                self.worker = status["renderer_pid"]
                self.record("Initial specified surface and background", status)
                self.missing = self.f.dest / "missing-color.exr"
                self.f.texture.parm("filename").set(str(self.missing))
                self.stage = "missing"
                return
            if self.stage == "missing":
                if not self.picture(status, (1, 0, 1), self.f.background, True):
                    return
                assert status["renderer_pid"] == self.worker
                assert any(self.missing.name in issue["message"] for issue in status["source_diagnostics"]), status
                self.record("Missing color input renders magenta without rejecting the scene", status)
                self.warning_geometry = status["geometry_conversions"]
                self.entry.apply(self.f.capture(), hou.time())
                self.stage = "warm-warning"
                self.hold = time.monotonic()
                return
            if self.stage == "warm-warning":
                if not self.picture(status, (1, 0, 1), self.f.background, True) or time.monotonic() - self.hold < 0.3:
                    return
                assert status["geometry_conversions"] == self.warning_geometry, status
                self.record("Warning survives warm capture without converting geometry", status)
                self.f.write_image(self.missing, (0.1, 0.6, 0.2))
                self.stage = "file-repair"
                return
            if self.stage == "file-repair":
                if not self.picture(status, (0.1, 0.6, 0.2), self.f.background, False):
                    return
                assert (
                    status["renderer_pid"] == self.worker and status["geometry_conversions"] == self.warning_geometry
                ), status
                self.record("Creating the missing file repairs the displayed render", status)
                self.f.geo.parm("shop_materialpath").set(self.f.foreign.path())
                self.stage = "foreign"
                return
            if self.stage == "foreign":
                if not self.picture(status, (1, 0, 1), self.f.background, True):
                    return
                assert any(issue["code"] == "material.unsupported" for issue in status["source_diagnostics"]), status
                self.record("Unfamiliar assigned shader stays visible", status)
                self.f.geo.parm("shop_materialpath").set(self.f.material.path())
                self.stage = "assignment-repair"
                return
            if self.stage == "assignment-repair":
                if not self.picture(status, (0.1, 0.6, 0.2), self.f.background, False):
                    return
                self.record("Repair assignment clears warnings", status)
                self.f.rop.setParms(dict(world_source="material", world_material=self.f.world.path()))
                self.stage = "world"
                return
            if self.stage == "world":
                if not self.picture(status, (0.1, 0.6, 0.2), self.f.world_color, False):
                    return
                self.record("Specified world radiance is displayed", status)
                self.pause(True)
                self.stage = "paused"
                return
            if self.stage == "paused":
                if status.get("backend_state") != "paused":
                    return
                self.record("Pause remains available while using source recovery", status)
                self.f.rop.parm("world_material").set("/mat/absent_transaction_world")
                self.hold = time.monotonic()
                self.stage = "paused-edit"
                return
            if self.stage == "paused-edit":
                if time.monotonic() - self.hold < 0.3:
                    return
                assert status.get("backend_state") == "paused" and status["renderer_pid"] == self.worker, status
                self.pause(False)
                self.stage = "world-broken"
                return
            if self.stage == "world-broken":
                if not self.picture(status, (0.1, 0.6, 0.2), self.f.background, True):
                    return
                assert any(issue["code"].startswith("world.") for issue in status["source_diagnostics"]), status
                assert status["renderer_pid"] == self.worker
                self.record("Resume displays configured background for a broken world", status)
                self.f.rop.parm("world_material").set(self.f.world.path())
                self.stage = "world-repair"
                return
            if self.stage == "world-repair":
                if not self.picture(status, (0.1, 0.6, 0.2), self.f.world_color, False):
                    return
                self.record("World repair clears diagnostics", status)
                self.second = self.f.dest / "second-missing.exr"
                self.f.texture.parm("filename").set(str(self.second))
                self.stage = "stop-warning"
                return
            if self.stage == "stop-warning":
                if not self.picture(status, (1, 0, 1), self.f.world_color, True):
                    return
                self.record("Stop controls a render with a source warning", status)
                self.stop()
                self.hold = time.monotonic()
                self.stage = "stopped"
                return
            if self.stage == "stopped":
                if status["state"] not in ("cancelled", "closed"):
                    return
                if time.monotonic() - self.hold < 0.2:
                    return
                if not hasattr(self, "repaired_while_stopped"):
                    self.f.write_image(self.second, (0.7, 0.3, 0.5))
                    self.repaired_while_stopped = True
                    self.hold = time.monotonic()
                    return
                if time.monotonic() - self.hold < 0.4:
                    return
                assert status["state"] in ("cancelled", "closed"), status
                self.record("File repair does not restart a stopped viewer", status)
                self.render()
                self.stage = "render-repaired"
                return
            if self.stage == "render-repaired":
                if not self.picture(status, (0.7, 0.3, 0.5), self.f.world_color, False):
                    return
                self.record("Render accepts repaired sources after Stop", status)
                self.retiring = self.session
                if self.panel:
                    self.panel.dispose()
                if self.viewer:
                    self.viewer.killRender()
                registry.close_all()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False
            if not self.finished:
                self.save()

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        if self.panel:
            self.panel.dispose()
        if self.viewer:
            self.viewer.killRender()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error, seconds=time.monotonic() - self.started)
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))
