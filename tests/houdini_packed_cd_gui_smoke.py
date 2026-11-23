# SPDX-License-Identifier: Apache-2.0
"""One live viewer: Cd owner edits/repairs while a million-triangle prototype stays retained.

HOM edits actual SOP/OBJ parameters and Qt clicks Render/Stop. This verifies
viewer integration, rather than physical input or end-to-end latency budgets.
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
from packed_cd_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_CD_VIEWER") == "native"
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
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.steps = [
            ("Packed primitive", lambda: None, False, None, False),
            ("Packed value edit", lambda: self.f.set_cd(offset=0.08), False, None, True),
            ("Packed value repair", lambda: self.f.set_cd(), False, None, True),
            ("Packed point", lambda: self.f.set_cd(owner="point"), False, None, True),
            ("Packed vertex", lambda: self.f.set_cd(owner="vertex"), False, None, True),
            ("Color Attribute", lambda: self.f.select_source(True), False, None, False),
            ("Color Attribute edit", lambda: self.f.set_cd(offset=0.06), False, None, True),
            ("Bad component", lambda: self.f.set_cd("bad_one"), True, None, False),
            ("Repair component", lambda: self.f.set_cd(), False, None, False),
            ("Bad type", lambda: self.f.set_cd("bad_type"), True, None, False),
            ("Repair type", lambda: self.f.set_cd(), False, None, False),
            ("Missing Cd", lambda: self.f.set_cd("missing"), True, None, False),
            ("Repair missing", lambda: self.f.set_cd(), False, None, False),
            ("Unnamed Color Attribute", lambda: self.f.select_source(True, True), False, None, False),
            ("Prototype primitive", lambda: self.prototype("primitive"), False, [(0.11, 0.51, 0.31)], False),
            ("Prototype vertex", lambda: self.prototype("vertex"), False, [(0.11, 0.51, 0.31)], False),
            ("Restore instance Cd", lambda: self.f.prototype_fields.parm("snippet").set(""), False, None, False),
            ("Unpacked primitive", lambda: self.f.set_cd(packed=False), False, None, False),
            ("Primitive edit", lambda: self.f.set_cd(packed=False, offset=0.09), False, None, False),
            ("Unpacked vertex", lambda: self.f.set_cd(packed=False, owner="vertex"), False, None, False),
            ("Vertex invalid", lambda: self.f.set_cd("bad_size", packed=False, owner="vertex"), True, None, False),
            ("Vertex repair", lambda: self.f.set_cd(packed=False, owner="vertex"), False, None, False),
            ("Unpacked point", lambda: self.f.set_cd(packed=False, owner="point"), False, None, False),
            ("Restore packed", lambda: self.f.set_cd(), False, None, False),
            ("Camera continues", lambda: self.f.camera.parm("tx").set(0.05), False, None, True),
            ("Object continues", lambda: self.f.obj.parm("tx").set(0.05), False, None, True),
        ]
        self.index = self.generation = 0
        self.panel = self.entry = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited_at = time.monotonic()
        self.report = dict(result="running", events=[], viewer="native" if self.native else "cycles", scope=__doc__)
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def prototype(self, owner):
        self.f.prototype_fields.parm("class").set(self.f.prototype_fields.parm("class").menuItems().index(owner))
        self.f.prototype_fields.parm("snippet").set("v@Cd={.11,.51,.31};")

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
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or time.monotonic() - self.edited_at < 0.35
            ):
                return
            if (
                status.get("pending_update")
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
            label, _, warning, expected, retained = self.steps[self.index]
            colors = self.f.colors(entry.session.frame(), expected, warning)
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            assert status["packed_instances"] in (12, 16) and len(registry.entries()) == 1, status
            if self.native:
                if not entry.helper_pid:
                    return
                plane = entry.session.frame()["planes"]["combined"]
                pixels = np.frombuffer(plane["pixels"], np.float32).reshape(
                    self.f.height, self.f.width, plane["channels"]
                )
                try:
                    if not np.allclose(self.tab.pixel("C", 43, 25)[:3], pixels[25, 43, :3], atol=3e-4, rtol=0):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert (
                    entry.views == 1 and not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
                )
            if self.report["events"]:
                before = self.report["events"][0]["status"]
                assert status["renderer_pid"] == before["renderer_pid"] and self.f.heavy.cookCount() == self.cooks, (
                    status
                )
                if retained:
                    assert (
                        status["geometry_conversions"] == self.report["events"][-1]["status"]["geometry_conversions"]
                    ), status
            self.cooks = self.f.heavy.cookCount()
            self.generation = info["generation"]
            self.report["events"].append(dict(label=label, colors=colors, status=status, heavy_cooks=self.cooks))
            self.save()
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
