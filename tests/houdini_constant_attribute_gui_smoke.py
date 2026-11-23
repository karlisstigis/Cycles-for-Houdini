# SPDX-License-Identifier: Apache-2.0
"""One live viewer: break/repair hashed point fields while retaining a heavy shared prototype."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from constant_attribute_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_CONSTANT_FIELDS_VIEWER") == "native"
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
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(950, 680))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.steps = [
            ("Packed reference", lambda: None, False),
            ("Unpacked copies", lambda: self.f.copies.parm("pack").set(0), False),
            ("Point value edit", lambda: self.f.configure(offset=0.037), False),
            ("Broken field", lambda: self.f.configure(broken=True), True),
            ("Repair field", lambda: self.f.configure(), False),
            ("Camera move", lambda: self.f.camera.parm("tx").set(0.05), False),
            ("Object move", lambda: self.f.obj.parm("tx").set(0.03), False),
            ("Vector hash", lambda: self.f.shade(field="seed3"), False),
            ("Vector edit", lambda: self.f.configure(offset=0.071), False),
            ("Packed vector", lambda: self.f.copies.parm("pack").set(1), False),
            ("Unpacked vector", lambda: self.f.copies.parm("pack").set(0), False),
            ("Float repair", lambda: self.restore(), False),
        ]
        self.report = dict(result="running", events=[], viewer="native" if self.native else "cycles", scope=__doc__)
        self.index = self.generation = 0
        self.panel = self.entry = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited = time.monotonic()
        self.baseline = None
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def restore(self):
        self.f.configure()
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
            assert time.monotonic() - self.began < 200, (self.phase, self.index)
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
            label, _, warning = self.steps[self.index]
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            reference = (
                self.baseline
                if label in ("Unpacked copies", "Repair field", "Camera move", "Object move", "Float repair")
                else None
            )
            values = self.f.groups(entry.session.frame(), reference=reference, warning=warning)
            if self.baseline is None:
                self.baseline = values
            if self.index:
                assert self.f.heavy.cookCount() == self.cooks, (self.cooks, self.f.heavy.cookCount())
            self.cooks = self.f.heavy.cookCount()
            self.generation = info["generation"]
            self.report["events"].append(dict(label=label, values=values, status=status, heavy_cooks=self.cooks))
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
