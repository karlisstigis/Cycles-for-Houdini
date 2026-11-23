# SPDX-License-Identifier: Apache-2.0
"""One live viewer: native ramp edits/repair with a retained million-triangle prototype.

HOM authors native ramp controls; Qt clicks the Cycles Render/Stop buttons.
This checks real viewer presentation and ownership, not physical mouse latency.
"""

from ramp_table_reference import native_lookup
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json, os, time, traceback
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_RAMP_VIEWER") == "native"
        self.builder = ch.create_material(name="ramp_live", shader="emission")
        self.emission = self.builder.node("surface_shader")
        self.emission.parm("strength").set(1)
        self.ramp = self.builder.createNode("cycles::color_ramp::1", "color_controls")
        self.emission.setNamedInput("color", self.ramp, "color")
        self.factor = self.builder.createNode("cycles::value::1", "factor")
        self.factor.parm("value").set(0.425)
        self.ramp.setNamedInput("factor", self.factor, "value")
        self.bases = (
            hou.rampBasis.Constant,
            hou.rampBasis.Linear,
            hou.rampBasis.CatmullRom,
            hou.rampBasis.MonotoneCubic,
            hou.rampBasis.Bezier,
            hou.rampBasis.BSpline,
            hou.rampBasis.Hermite,
        )
        self.keys = (0.05, 0.12, 0.3, 0.45, 0.67, 0.85, 0.95)
        self.values = (
            (0.2, 0.4, 0.8),
            (0.8, 0.2, 0.1),
            (0.1, 0.9, 0.4),
            (0.6, 0.3, 0.2),
            (0.9, 0.2, 0.6),
            (0.2, 0.7, 0.3),
            (0.7, 0.1, 0.4),
        )
        self.set_ramp(2)
        obj = hou.node("/obj").createNode("geo", "receiver", run_init_scripts=False)
        grid = obj.createNode("grid")
        grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
        grid.parmTuple("size").set((8, 8))
        grid.setRenderFlag(True)
        self.heavy = hou.node("/obj").createNode("geo", "retained_instances", run_init_scripts=False)
        self.torus = self.heavy.createNode("torus")
        self.torus.setParms(dict(type=0, rows=512, cols=1024))
        targets = self.heavy.createNode("grid")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("t").set((0, 0, 20))
        copies = self.heavy.createNode("copytopoints")
        copies.setInput(0, self.torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setRenderFlag(True)
        copies.setDisplayFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "live_ramp")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                camera=self.camera.path(),
                materialpath=self.builder.path(),
                vobject=obj.path() + " " + self.heavy.path(),
                resx=160,
                resy=96,
                samples=1,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.rop.parmTuple("lightstrength").set((0, 0, 0))
        self.key = f"rop:{self.rop.sessionId()}"
        self.steps = [("Initial", lambda: None)]
        for basis in range(7):
            self.steps.append(("Basis " + str(basis), lambda basis=basis: self.set_ramp(basis)))
        for space in (hou.colorType.LAB, hou.colorType.RGB):
            self.steps.append(("Color space " + str(space), lambda space=space: self.set_ramp(4, space)))
        for cycle in range(3):
            self.steps.append(("Broken knots " + str(cycle), lambda: self.ramp.parm("ramp4pos").set(0.3)))
            self.steps.append(("Repair " + str(cycle), lambda: self.ramp.parm("ramp4pos").set(0.45)))
        self.steps.extend(
            [
                ("Camera move", lambda: self.camera.parm("tx").set(0.1)),
                ("Instance move", lambda: self.heavy.parm("tx").set(0.5)),
                ("Factor edit", lambda: self.factor.parm("value").set(0.2125)),
                ("Ramp rename", lambda: self.ramp.setName("renamed_live_ramp")),
            ]
        )
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(600, 620))
        self.settings.setPin(True)
        self.settings.setCurrentNode(self.ramp)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.index = self.generation = 0
        self.phase = "mount"
        self.entry = self.session = None
        self.began = time.monotonic()
        self.busy = self.finished = False
        self.report = dict(result="running", events=[], viewer="native" if self.native else "cycles", scope=__doc__)
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def set_ramp(self, basis, space=hou.colorType.RGB):
        ramp = hou.Ramp((self.bases[basis],) * 7, self.keys, self.values)
        ramp.setColorType(space)
        self.ramp.parm("ramp").set(ramp)
        self.ramp.parm("ramp_color_space").set(str(space).split(".")[-1])

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
                    self.panel.set_rop(self.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            entry = registry.get(self.key)
            if entry is None:
                return
            self.entry = entry
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                not info
                or not info["complete"]
                or info["generation"] <= self.generation
                or info["generation"] != status["generation"]
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            warning = self.steps[self.index][0].startswith("Broken")
            reference = self.ramp.parm("ramp").evalAsRamp()
            reference.setColorType(getattr(hou.colorType, self.ramp.evalParm("ramp_color_space")))
            expected = (1, 0, 1) if warning else native_lookup(reference, self.factor.evalParm("value"))
            frame = entry.session.frame()
            plane = frame["planes"]["combined"]
            image = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])
            np.testing.assert_allclose(image[48, 80, :3], expected, atol=4e-5, rtol=4e-5)
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            if self.native:
                if not entry.helper_pid:
                    return
                try:
                    if not np.allclose(self.tab.pixel("C", 80, 48)[:3], expected, atol=4e-5, rtol=4e-5):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert status["packed_instances"] == 12 and len(registry.entries()) == 1, status
            if self.report["events"]:
                before = self.report["events"][0]["status"]
                assert (
                    status["renderer_pid"] == before["renderer_pid"]
                    and status["geometry_conversions"] == before["geometry_conversions"]
                    and self.torus.cookCount() == self.cooks
                ), status
            self.cooks = self.torus.cookCount()
            self.generation = info["generation"]
            self.report["events"].append(
                dict(label=self.steps[self.index][0], status=status, expected=list(expected), heavy_cooks=self.cooks)
            )
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.steps[self.index][1]()
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
