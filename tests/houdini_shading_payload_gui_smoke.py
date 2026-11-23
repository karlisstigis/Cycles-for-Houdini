# SPDX-License-Identifier: Apache-2.0
"""Live field repair, retained subdivision and Stop/Render beside heavy shared geometry."""

from subdivision_test_setup import configure_render, set_subdivision
import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry, object_properties
from pointiness_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_SHADING_VIEWER") == "native"
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.f.set_shape("plane", deform=0.25, seams=True)
        self.fields = self.f.obj.createNode("python", "artist_fields")
        self.fields.setInput(0, self.f.stash)
        group = self.fields.parmTemplateGroup()
        group.append(hou.FloatParmTemplate("value", "Value", 1, default_value=(0.37,)))
        group.append(hou.ToggleParmTemplate("broken", "Broken", default_value=False))
        self.fields.setParmTemplateGroup(group)
        self.fields.parm("python").set("""node=hou.pwd();geo=node.geometry()
geo.addAttrib(hou.attribType.Point,'rand',0.)
value=float('nan') if node.parm('broken').eval() else node.parm('value').eval()
for point in geo.points():point.setAttribValue('rand',value)
""")
        self.f.wave.setInput(0, self.fields)
        self.f.user.parm("attribute").set("rand")
        self.f.shade("user")
        self.f.displace()
        heavy = self.f.add_heavy()
        object_properties.install(heavy)
        heavy.setParms(dict(cycles_subdivision_override=0))
        self.steps = []
        for scheme in ("none", "linear", "catmull_clark"):
            for packed in (False, True):
                for kind, value in (
                    ("baseline", 0.37),
                    ("value", 0.61),
                    ("broken", None),
                    ("repair", 0.82),
                    ("height", 0.82),
                ):
                    self.steps.append(dict(kind=kind, value=value, scheme=scheme, packed=packed))
        self.steps.append(dict(kind="restart", value=0.73, scheme="catmull_clark", packed=True))
        self.report = dict(result="running", viewer="native" if self.native else "cycles", events=[], scope=__doc__)
        self.key = f"rop:{self.f.rop.sessionId()}"
        self.index = self.generation = 0
        self.panel = self.entry = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.began = self.edited = time.monotonic()
        self.edit()
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(950, 700))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def edit(self):
        step = self.steps[self.index]
        kind = step["kind"]
        if kind == "baseline":
            self.f.output.parm("input").set(int(step["packed"]))
            configure_render(
                self.f.rop, dict(subdivision=dict(type=step["scheme"], space="object", dicing_rate=0.25, max_level=3))
            )
            self.f.displacement.parm("scale").set(0.2)
        if kind == "height":
            self.f.displacement.parm("scale").set(0.35)
        if kind == "restart":
            self.reference = self.last_planes
            self.counters = self.last_counters
            self.stopped_session = self.entry.session
            self.fields.parm("value").set(0.44)
            if self.native:
                self.tab.killRender()
            else:
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
        self.fields.parm("broken").set(kind == "broken")
        if step["value"] is not None:
            self.fields.parm("value").set(step["value"])
        if kind == "restart":
            self.phase = "restart_wait"
        self.edited = time.monotonic()

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
            if self.phase == "restart_wait" and self.native:
                # Stopping the only native display retires its session. Verify
                # cleanup, then start a new owner; panel Stop retains its owner.
                retired = self.stopped_session.status()
                if retired["state"] != "closed":
                    return
                assert retired["owned_geometry_bytes"] == retired["device_allocation_bytes"] == 0, retired
                self.report["native_stop_closed"] = retired
                self.tab.startRender()
                self.generation = 0
                self.phase = "wait"
                self.edited = time.monotonic()
                return
            self.entry = registry.get(self.key)
            if not self.entry:
                return
            self.session = self.entry.session
            status = self.entry.session.status()
            info = self.entry.session.frame_info()
            assert not self.entry.error and status["state"] != "failed", (self.entry.error, status)
            if self.phase == "restart_wait":
                if status.get("capture_pending") or status.get("backend_state", status["state"]) != "cancelled":
                    return
                if self.native:
                    self.tab.startRender()
                else:
                    if not self.panel.render_button.isEnabled():
                        return
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                self.edited = time.monotonic()
                return
            if (
                self.entry.change_serial != self.entry.applied_serial
                or self.entry.requested_serial
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
            if self.native and not self.entry.helper_pid:
                return
            step = self.steps[self.index]
            kind = step["kind"]
            assert bool(status["source_diagnostics"]) == (kind == "broken"), status["source_diagnostics"]
            planes = self.f.planes(self.entry.session.frame())
            visible = planes["combined"][:, :, 3] > 0.999
            assert visible.sum() > 40
            assert all(np.isfinite(plane).all() for plane in planes.values()), step
            expected = (1, 0, 1) if kind == "broken" else (step["value"],) * 3
            np.testing.assert_allclose(
                planes["combined"][visible, :3],
                np.broadcast_to(expected, planes["combined"][visible, :3].shape),
                rtol=0,
                atol=3e-5,
            )
            counters = (status["displacement_rebuilds"], status["subdivision_tessellations"])
            if kind == "baseline":
                self.reference = planes
                self.counters = counters
            elif kind == "height":
                assert (
                    counters[0] > self.counters[0]
                    and np.max(np.abs(planes["position"] - self.reference["position"])) > 1e-5
                ), (step, counters, self.counters)
            elif kind == "restart" and self.native:
                assert self.entry.session is not self.stopped_session and counters == (1, 1), (step, counters)
                np.testing.assert_allclose(planes["position"], self.reference["position"], rtol=0, atol=3e-6)
            else:
                assert counters == self.counters, (step, counters, self.counters)
                for name in ("position", "normal"):
                    np.testing.assert_allclose(planes[name], self.reference[name], rtol=0, atol=3e-6, err_msg=str(step))
            assert status["geometry_resources"] == status["mesh_variant_resources"] == 2, status
            assert status["object_resources"] == (16 if step["packed"] else 13), status
            if self.index:
                assert self.f.heavy.cookCount() == self.cooks, (self.cooks, self.f.heavy.cookCount())
            self.cooks = self.f.heavy.cookCount()
            self.generation = info["generation"]
            self.last_planes = planes
            self.last_counters = counters
            self.report["events"].append(dict(step=step, status=status, heavy_cooks=self.cooks))
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.edit()
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
        self.session = self.entry.session if self.entry else self.session
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
