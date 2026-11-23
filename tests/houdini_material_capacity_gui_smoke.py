# SPDX-License-Identifier: Apache-2.0
"""One heavy viewer: repair excessive native layers, repair an image and move the camera."""

import json, os, time, traceback
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from light_shader_fixture import write_image
from houdini_material_override_gui_smoke import Workflow as OverrideWorkflow


class Workflow(OverrideWorkflow):
    def __init__(self):
        super().__init__(auto_start=False)
        self.report["scope"] = __doc__
        self.report["capacity_workflow"] = True
        self.report["process_pid"] = os.getpid()
        self.f.obj.parm("shop_materialpath").set(self.material.path())
        detail = self.f.obj.createNode("attribwrangle", "retained_detail_layer")
        detail.setInput(0, self.sop)
        detail.setParms(dict(**{"class": 0}, snippet="s@material_override=\"{'power':1}\";"))
        self.control = self.f.obj.createNode("attribwrangle", "capacity_control")
        self.control.setInput(0, detail)
        self.control.addSpareParmTuple(hou.IntParmTemplate("distinct", "Distinct Overrides", 1, default_value=(5000,)))
        self.control.setParms(
            dict(
                **{"class": 1},
                snippet='s@material_override=sprintf("{\'power\':%.8f}",2.0+float(@primnum%chi("distinct"))*0.000001);',
            )
        )
        self.control.setDisplayFlag(True)
        self.control.setRenderFlag(True)
        self.settings.setCurrentNode(self.control)
        if self.native:
            self.tab.startRender()

    def tick(self):
        if self.phase in ("mount", "focus", "keys", "commit"):
            return super().tick()
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            self.report["tick_intervals"].append(dict(phase=self.phase, seconds=now - self.last_tick))
            self.last_tick = now
            assert now - self.began < 200, self.phase
            if now - self.saved_at > 0.5:
                self.report["phase"] = self.phase
                self.path.write_text(json.dumps(self.report), encoding="utf-8")
                self.saved_at = now
            if now < self.next_at:
                return
            if self.commit_deadline:
                value = self.parm.eval()
                matches = value == self.text if isinstance(value, str) else float(value) == float(self.text)
                assert matches or now < self.commit_deadline, (self.parm.path(), value, self.text)
                if not matches:
                    return
                self.commit_deadline = None
            entry = registry.get(f"rop:{self.f.rop.sessionId()}")
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert status["state"] != "failed" and not entry.error, (status, entry.error)
            if (
                not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] <= self.generation
                or status.get("pending_update")
                or status.get("capture_pending")
            ):
                return
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (64, 64):
                    return
                try:
                    shown = np.asarray(self.tab.pixel("C", 32, 32)[:3])
                except hou.OperationFailed:
                    return
                if not np.allclose(shown, self.expected, atol=2e-5, rtol=0):
                    return
            elif self.panel._live_generation != info["generation"]:
                return
            frame = entry.session.frame()
            np.testing.assert_allclose(
                self.f.pixels(frame)[31:33, 31:33, :3], np.broadcast_to(self.expected, (2, 2, 3)), atol=2e-5, rtol=0
            )
            assert (
                len(registry.entries()) == 1 and status["packed_instances"] == 12 and status["geometry_resources"] == 2
            ), status
            codes = {d["code"] for d in status["source_diagnostics"]}
            expected = set() if self.phase == "repaired" else {"material.override_capacity"}
            if self.phase == "file_broken":
                expected.add("texture.unavailable")
            assert codes == expected, status
            for issue in status["source_diagnostics"]:
                if issue["code"] == "material.override_capacity":
                    assert issue["source"] == self.control.path() and issue["attribute"] == "material_override", issue
            if self.phase == "first":
                self.pid = status["renderer_pid"]
                self.conversions = status["geometry_conversions"]
                self.rebuilds = status["displacement_rebuilds"]
                self.record(
                    "Over-capacity layers retain the heavy displaced mesh and useful native source diagnostics", status
                )
                self.edit(self.control.parm("distinct"), 1, "repaired", (0.4, 0.8, 1.2))
                return
            assert (
                status["renderer_pid"] == self.pid
                and status["geometry_conversions"] == self.conversions
                and status["displacement_rebuilds"] == self.rebuilds
            ), status
            if self.phase == "repaired":
                self.record(
                    "Native parameter keyboard repair restores the high-priority layer and clears capacity warnings",
                    status,
                )
                self.edit(self.control.parm("distinct"), 5000, "overflow_again", (0.2, 0.4, 0.6))
                return
            if self.phase == "overflow_again":
                self.record("Repeated excessive population keeps rendering through a local fallback", status)
                self.edit(self.material.parm("image_file"), str(self.missing), "file_broken", (1, 0, 1))
                return
            if self.phase == "file_broken":
                self.record("A concurrent missing color image preserves capacity recovery and displacement", status)
                write_image(self.missing, (0.2, 0.4, 0.6))
                self.expected = (0.2, 0.4, 0.6)
                self.phase = "file_repaired"
                return
            if self.phase == "file_repaired":
                self.record("Automatic file repair clears only the texture warning", status)
                self.edit(self.f.camera.parm("tx"), 0.5, "camera", (0.2, 0.4, 0.6))
                return
            if self.phase == "camera":
                self.record("Camera navigation retains heavy geometry and the current capacity warning", status)
                capture = self.path.parent / "capacity-viewer.png"
                window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
                self.report["viewer_capture"] = str(capture)
                self.report["viewer_capture_saved"] = window.grab().save(str(capture))
                if self.native:
                    self.tab.killRender()
                else:
                    QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False


if __name__ == "__main__":
    workflow = Workflow()
