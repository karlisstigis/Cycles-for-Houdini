# SPDX-License-Identifier: Apache-2.0
"""One live viewer: OBJ shutter edits/repairs and independent analytic images."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry, object_properties
from sampled_override_fixture import Fixture
from independent_binding_fixture import motion_pixels


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_OBJECT_SHUTTER_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.camera = hou.node(f.rop.evalParm("camera"))
        f.assigned.parm("num_local1").set(0)
        f.output.setInput(0, f.sop)
        f.output.parm("snippet").set("")
        f.sop.parm("snippet").set("v@N=set(1,0,0);v@v=set(0,0,0);")
        f.rop.setParms(dict(geometry_motion="transform", motion_samples=3))
        f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
        self.control = f.obj.parm("shop_materialstylesheet")
        if f.obj.parm("material_override") is None:
            f.obj.addSpareParmTuple(hou.StringParmTemplate("material_override", "Material Override", 1))
        self.override = f.obj.parm("material_override")
        self.style()
        self.future = self.path.parent / "future-style.json"
        heavy = hou.node("/obj").createNode("geo", "object_shutter_resident", run_init_scripts=False)
        object_properties.install(heavy)
        heavy.parm("cycles_geometry_motion").set("transform")
        self.heavy = heavy.createNode("torus", "million_triangle_prototype")
        self.heavy.setParms(dict(type=0, rows=512, cols=1024))
        targets = heavy.createNode("grid", "targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((18, 0, -8))
        copies = heavy.createNode("copytopoints", "twelve_instances")
        copies.setInput(0, self.heavy)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        material = ch.create_material(name="object_shutter_resident_material", shader="diffuse_bsdf")
        heavy.parm("shop_materialpath").set(material.path())
        f.rop.parm("vobject").set(f.obj.path() + " " + heavy.path())
        self.other = self.make_other()
        self.other_path = self.other.path()
        # Expected images are analytic box-shutter coverage, not a second render.
        self.steps = []

        def add(label, action, centres=(-0.75, 0, 0.75), warning=False, power=2, shift=0, retain=False):
            self.steps.append((label, action, motion_pixels(centres, shift) * power, warning, power, retain))

        add("Animated object stylesheet", lambda: None)
        add("Five object shutter samples", lambda: f.rop.parm("motion_samples").set(5), np.linspace(-0.75, 0.75, 5))
        add("Surface-only object edit", lambda: self.style(power=3), np.linspace(-0.75, 0.75, 5), power=3, retain=True)
        add(
            "Invalid off-frame height",
            lambda: self.style(broken=True),
            np.minimum(np.linspace(-0.75, 0.75, 5), 0),
            True,
        )
        add("Repair off-frame height", self.style, np.linspace(-0.75, 0.75, 5))
        add("Malformed off-frame JSON", self.malformed, np.minimum(np.linspace(-0.75, 0.75, 5), 0), True)
        add("Repair object JSON", self.style, np.linspace(-0.75, 0.75, 5))
        add("Animated object local override", lambda: self.local_override(False), np.linspace(-0.75, 0.75, 5))
        add(
            "Invalid object local override",
            lambda: self.local_override(True),
            np.minimum(np.linspace(-0.75, 0.75, 5), 0),
            True,
        )
        add("Repair local override", lambda: self.local_override(False), np.linspace(-0.75, 0.75, 5))
        add("Restore scoped object rules", self.restore_styles, np.linspace(-0.75, 0.75, 5))
        add("Animated object material path", self.animate_path, np.linspace(-0.75, 0.75, 5))
        add("Delete off-frame material", lambda: self.other.destroy(), (0, 0, 0, 0, 0), True)
        add("Recreate off-frame material", self.recreate_other, np.linspace(-0.75, 0.75, 5))
        add("Restore object assignment", self.restore_path, np.linspace(-0.75, 0.75, 5))
        add("Positive offset shutter", lambda: self.offset(1), np.linspace(-0.75, 0.75, 5))
        add("Negative offset shutter", lambda: self.offset(-1), np.linspace(-0.75, 0.75, 5))
        add("Velocity with object shader motion", self.velocity, np.linspace(-0.75, 0.75, 5))
        add(
            "Deformation with object shader motion",
            lambda: f.rop.parm("geometry_motion").set("deformation"),
            np.linspace(-0.75, 0.75, 5),
        )
        add(
            "Deformation off-frame repair fallback",
            lambda: self.style(broken=True),
            np.minimum(np.linspace(-0.75, 0.75, 5), 0),
            True,
        )
        add("Deformation object repair", self.style, np.linspace(-0.75, 0.75, 5))
        add("Return to transform policy", lambda: f.rop.setParms(dict(geometry_motion="transform", motion_samples=3)))
        add("Import used only off-frame", self.future_rule)
        add("Edit off-frame import without parameter edit", lambda: self.write_future(1), (-0.75, 0, 0.5))
        add("Delete off-frame import", lambda: self.future.unlink(), (-0.75, 0, 0), True)
        add("Recreate off-frame import", lambda: self.write_future(1.25))
        add("Malformed off-frame import", lambda: self.future.write_text("{broken"), (-0.75, 0, 0), True)
        add("Repair off-frame import", lambda: self.write_future(0.75), (-0.75, 0, 0.25))
        add("Restore inline object rule", self.style)
        add("Object source capacity fallback", self.capacity_rule, (0, 0, 0), True)
        times = np.linspace(-0.75, 0.75, 129)
        add(
            "Repair source capacity without parameter edit",
            lambda: self.write_future(1.25),
            np.where(times > 0, 0.75, times),
        )
        add("Restore regular source sampling", self.regular_rule)
        add("Camera navigation", lambda: self.camera.parm("tx").set(0.05), shift=-2, retain=True)
        add("Object navigation", lambda: f.obj.parm("tx").set(-0.45), retain=True)
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            events=[],
            physical_mouse_certification=False,
        )
        self.index = 0
        self.phase = "mount"
        self.began = time.monotonic()
        self.next_at = 0
        self.previous_generation = 0
        self.busy = self.finished = False
        self.entry = self.panel = None
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 500))
        self.settings.setPin(True)
        self.settings.setCurrentNode(f.obj)
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
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def style(self, broken=False, power=2):
        self.control.setExpression(
            'import json\nreturn json.dumps({"styles":[{"overrides":{"surfaceParameters":{"power":'
            + str(power)
            + '},"displaceParameters":{"height":'
            + ('"bad" if hou.time()>0 else ' if broken else "")
            + ".5+hou.time()*72}}}]})",
            hou.exprLanguage.Python,
        )

    def malformed(self):
        self.style()
        self.control.setExpression(
            self.control.expression().replace(
                "return json.dumps", 'if hou.time()>0:return "{broken"\nreturn json.dumps'
            ),
            hou.exprLanguage.Python,
        )

    def local_override(self, broken):
        self.control.deleteAllKeyframes()
        self.control.set("")
        self.override.setExpression(
            'repr({"power":2,"height":' + ('"bad" if hou.time()>0 else ' if broken else "") + ".5+hou.time()*72})",
            hou.exprLanguage.Python,
        )

    def restore_styles(self):
        self.override.deleteAllKeyframes()
        self.override.set("")
        self.style()

    def make_other(self):
        material = ch.create_material(name="object_shutter_live_other", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        material.node("surface_shader").setParms(dict(strength=10.0))
        shader = material.node("surface_shader")
        shader.parmTuple("color").set((0.9, 0.1, 0.2))
        node = material.createNode("cycles::displacement::1", "height")
        node.setParms(dict(height=0.5, scale=1, midlevel=0, space="object"))
        material.node("surface_output").setNamedInput("displacement", node, "displacement")
        material.parm("cycles_displacement_method").set("true")
        ch.promote_parameter(material, node, "height", name="height")
        ch.promote_parameter(material, shader, "strength", name="power")
        return material

    def animate_path(self):
        self.f.obj.parm("shop_materialpath").setExpression(
            repr(self.f.material.path()) + " if hou.time()<=0 else " + repr(self.other_path), hou.exprLanguage.Python
        )

    def recreate_other(self):
        self.other = self.make_other()

    def restore_path(self):
        self.f.obj.parm("shop_materialpath").deleteAllKeyframes()
        self.f.obj.parm("shop_materialpath").set(self.f.material.path())

    def offset(self, value):
        self.f.rop.parm("shutteroffset").set(value)
        self.camera.parm("tx").set(value * 0.75)

    def velocity(self):
        self.offset(0)
        self.f.rop.parm("geometry_motion").set("velocity")

    def write_future(self, height, padding=False):
        self.future.write_text(
            json.dumps(
                {
                    "styles": [
                        {
                            "overrides": {
                                "surfaceParameters": {"power": 2},
                                "displaceParameters": {"height": height},
                                **({"materialParameters": {"unused_large_value": "x" * 900000}} if padding else {}),
                            }
                        }
                    ]
                }
            )
        )

    def future_rule(self, padding=False):
        self.write_future(1.25, padding)
        self.style()
        self.control.setExpression(
            self.control.expression().replace(
                "return json.dumps",
                'if hou.time()>0:return json.dumps({"importFiles":['
                + repr(self.future.as_posix())
                + "]})\nreturn json.dumps",
                1,
            ),
            hou.exprLanguage.Python,
        )

    def capacity_rule(self):
        self.f.rop.parm("motion_samples").set(129)
        self.future_rule(True)

    def regular_rule(self):
        self.f.rop.parm("motion_samples").set(3)
        self.style()

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
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                if self.callback in hou.ui.eventLoopCallbacks():
                    hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 300, (self.phase, self.index)
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            if time.monotonic() < self.next_at:
                return
            self.entry = entry = registry.get("rop:" + str(self.f.rop.sessionId()))
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status["generation"] <= self.previous_generation
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            label, _, expected, warning, power, retain = self.steps[self.index]
            pixels = self.f.pixels(entry.session.frame())["combined"][:, :, :3]
            difference = float(np.abs(pixels - expected).mean())
            self.report["waiting"] = dict(label=label, mean_difference=difference, status=status)
            self.save()
            if difference / power >= 0.0008:
                return
            assert bool(status["source_diagnostics"]) == warning and not status["motion_warnings"], status
            if label == "Object source capacity fallback":
                assert any("8 MiB" in issue["message"] for issue in status["source_diagnostics"]), status
                assert "file:" + self.future.as_posix() in entry.session.native_stylesheet_resources()
            assert (
                status["geometry_resources"] == 2 and status["packed_instances"] == 12 and len(registry.entries()) == 1
            ), status
            if self.native:
                if not entry.helper_pid or self.tab.imageResolution() != (160, 90):
                    return
                try:
                    if any(
                        not np.allclose(self.tab.pixel("C", x, y)[:3], pixels[y, x], atol=3e-5, rtol=3e-5)
                        for x, y in ((55, 45), (80, 45), (128, 45), (150, 45))
                    ):
                        return
                except hou.OperationFailed:
                    return
            if self.report["events"]:
                first = self.report["events"][0]
                assert (
                    status["renderer_pid"] == first["status"]["renderer_pid"]
                    and self.heavy.cookCount() == first["heavy_cooks"]
                ), status
                if retain:
                    assert (
                        status["displacement_rebuilds"] == self.report["events"][-1]["status"]["displacement_rebuilds"]
                    ), label
                if label in ("Camera navigation", "Object navigation"):
                    assert (
                        status["geometry_conversions"] == self.report["events"][-1]["status"]["geometry_conversions"]
                    ), label
            self.report["events"].append(
                dict(label=label, mean_difference=difference, status=status, heavy_cooks=self.heavy.cookCount())
            )
            self.save()
            self.previous_generation = status["generation"]
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.steps[self.index][1]()
            self.next_at = time.monotonic() + 0.15
        except BaseException:
            self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        if self.phase == "closing":
            self.report.update(result="failed", error=error)
            self.finished = True
            self.save()
            if self.callback in hou.ui.eventLoopCallbacks():
                hou.ui.removeEventLoopCallback(self.callback)
            hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            return
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
