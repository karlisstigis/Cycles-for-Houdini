# SPDX-License-Identifier: Apache-2.0
"""One live viewer: intrinsic curvature, independent fields, recovery and retained heavy geometry."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from pointiness_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_POINTINESS_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.add_heavy()
        self.key = f"rop:{self.f.rop.sessionId()}"
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(950, 700))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.steps = [
            ("Convex analytic", lambda: None, 0.75, False, 6),
            ("Deformed original quads", lambda: self.f.set_shape("plane", deform=0.25), None, False, 9),
            ("Independent reference", lambda: self.f.shade("reference"), None, False, 0),
            ("Intrinsic repair", lambda: self.f.shade(), None, False, 9),
            (
                "Invalid authored normals",
                lambda: self.f.set_shape("plane", deform=0.25, normals="bad_type"),
                None,
                True,
                9,
            ),
            ("Normal repair", lambda: self.f.set_shape("plane", deform=0.25), None, False, 9),
            ("Packed copies", lambda: self.packed(), 0.75, False, 6),
            ("Release demand", lambda: self.f.shade("constant"), 0.3, False, 0),
            ("Restore demand", lambda: self.f.shade(), 0.75, False, 6),
            ("Mixed domains", lambda: self.f.output.parm("input").set(2), 0.75, True, 6),
            ("Repair mixed shader", lambda: self.f.shade("constant"), 0.3, False, 0),
            ("Restore mesh", lambda: self.restore(), 0.75, False, 6),
            ("Camera move", lambda: self.f.camera.parm("tx").set(0.05), 0.75, False, 6),
            ("Object move", lambda: self.f.obj.parm("tx").set(0.03), 0.75, False, 6),
            ("Intrinsic displacement", lambda: self.displace(), 0.75, False, 6),
            ("Broken independent color", lambda: self.color(True), None, True, 6),
            ("Repair independent color", lambda: self.color(False), 0.37, False, 6),
            ("Restore surface", lambda: self.restore_surface(), 0.75, False, 6),
        ]
        self.report = dict(result="running", events=[], viewer="native" if self.native else "cycles", scope=__doc__)
        self.index = self.generation = 0
        self.panel = self.entry = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited = time.monotonic()
        self.reference = None
        self.displaced = None
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def packed(self):
        self.f.set_shape()
        self.f.output.parm("input").set(1)

    def restore(self):
        self.f.output.parm("input").set(0)
        self.f.shade()

    def displace(self):
        self.f.set_shape(normals="radial", user_value=0.37)
        self.f.displace()

    def color(self, broken):
        self.f.set_shape(normals="radial", user_value=float("nan") if broken else 0.37)
        self.f.shade("user")

    def restore_surface(self):
        self.f.displace(False)
        self.f.shade()

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
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 240, (self.phase, self.index)
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            entry = registry.get(self.key)
            if not entry:
                return
            self.entry = entry
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or time.monotonic() - self.edited < 0.3
            ):
                return
            if (
                status.get("pending_update")
                or status.get("capture_pending")
                or not info
                or not info["complete"]
                or info["generation"] <= self.generation
                or info["generation"] != status["generation"]
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            if self.native and not entry.helper_pid:
                return
            label, _, value, warning, demand = self.steps[self.index]
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            frame = entry.session.frame()
            planes = self.f.planes(frame)
            owned = self.f.owned(entry.session)
            assert owned["pointiness_vertices"] == demand, (label, owned)
            if value is not None:
                self.f.gray(frame, value, magenta=warning)
            if label == "Deformed original quads":
                self.reference = planes["combined"]
            if label in ("Independent reference", "Intrinsic repair", "Invalid authored normals", "Normal repair"):
                np.testing.assert_allclose(planes["combined"], self.reference, rtol=0, atol=2e-6)
            if label == "Intrinsic displacement":
                self.displaced = planes["position"]
            if label in ("Broken independent color", "Repair independent color"):
                np.testing.assert_allclose(planes["position"], self.displaced, rtol=0, atol=2e-6)
            if self.index:
                assert self.f.heavy.cookCount() == self.cooks, (self.cooks, self.f.heavy.cookCount())
            self.cooks = self.f.heavy.cookCount()
            self.generation = info["generation"]
            self.report["events"].append(dict(label=label, status=status, owned=owned, heavy_cooks=self.cooks))
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.steps[self.index][1]()
            self.edited = time.monotonic()
        except BaseException:
            if self.phase == "closing":
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
