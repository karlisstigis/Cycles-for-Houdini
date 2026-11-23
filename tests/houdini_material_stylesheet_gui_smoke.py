# SPDX-License-Identifier: Apache-2.0
"""One live viewer: native styles, repairs and packed hierarchy retain unrelated geometry."""

import pass_fixtures as pass_fixture

import ast, json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry
from light_shader_fixture import Fixture


def sheet(overrides, target=None):
    return json.dumps(dict(styles=[dict(target=target or {}, overrides=overrides)]))


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_STYLES_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.add_heavy_packed()
        self.heavy = f.heavy.node("million_triangle_prototype")
        pass_fixture.set_parameters(f.rop, dict(use_material_assignments=1, samples=1, ipr_samples=1, pass_position=1))
        pass_fixture.parm(f.light, "light_intensity").set(0)
        self.mat = ch.create_material(name="live_stylesheet", shader="emission")
        shader = self.mat.node("surface_shader")
        shader.parmTuple("color").set((0.2, 0.4, 0.6))
        pass_fixture.parm(shader, "strength").set(1)
        ch.promote_parameter(self.mat, shader, "strength", name="power")
        pass_fixture.parm(f.obj, "shop_materialpath").set(self.mat.path())
        f.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Style Sheet", 1))
        self.control = pass_fixture.parm(f.obj, "shop_materialstylesheet")
        self.missing = "/mat/live_stylesheet_repair"
        self.heavy_material = heavy_material = ch.create_material(
            name="stylesheet_heavy_diffuse", shader="diffuse_bsdf"
        )
        ch.promote_parameter(heavy_material, heavy_material.node("surface_shader"), "roughness", name="roughness")
        pass_fixture.parm(f.heavy, "shop_materialpath").set(heavy_material.path())
        f.heavy.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Style Sheet", 1))
        self.heavy_style = pass_fixture.parm(f.heavy, "shop_materialstylesheet")
        self.steps = [
            ("Underlying assignment", lambda: None, (0.2, 0.4, 0.6), False),
            (
                "Object numeric override",
                lambda: self.control.set(sheet({"materialParameters": {"power": 2}})),
                (0.4, 0.8, 1.2),
                False,
            ),
            (
                "Bad parameter keeps valid values",
                lambda: self.control.set(sheet({"materialParameters": {"power": 3, "bad_field": 1}})),
                (0.6, 1.2, 1.8),
                True,
            ),
            (
                "Parameter repair",
                lambda: self.control.set(sheet({"materialParameters": {"power": 3}})),
                (0.6, 1.2, 1.8),
                False,
            ),
            ("Malformed source keeps geometry", lambda: self.control.set("{broken"), (0.2, 0.4, 0.6), True),
            ("Malformed source repair", lambda: self.control.set(""), (0.2, 0.4, 0.6), False),
            (
                "Missing assigned material",
                lambda: self.control.set(sheet({"material": {"name": self.missing}})),
                (1, 0, 1),
                True,
            ),
            ("Create material while rendering", self.repair_material, (0.7, 0.15, 0.3), False),
            ("Return to underlying material", lambda: self.control.set(""), (0.2, 0.4, 0.6), False),
            (
                "Uniform rule on heavy shared prototype",
                lambda: self.heavy_style.set(sheet({"materialParameters": {"roughness": 0.3}}, dict(type="self"))),
                (0.2, 0.4, 0.6),
                False,
            ),
            (
                "Edit uniform heavy rule",
                lambda: self.heavy_style.set(sheet({"materialParameters": {"roughness": 0.6}}, dict(type="self"))),
                (0.2, 0.4, 0.6),
                False,
            ),
            (
                "Broken heavy rule keeps geometry",
                lambda: self.heavy_style.set(
                    sheet({"materialParameters": {"roughness": "bad_value"}}, dict(type="self"))
                ),
                (0.2, 0.4, 0.6),
                True,
            ),
            ("Heavy rule repair", lambda: self.heavy_style.set(""), (0.2, 0.4, 0.6), False),
            ("Animated rule at frame 1", self.animate, (0.4, 0.8, 1.2), False),
            ("Frame 2 updates rule", lambda: hou.setFrame(2), (0.6, 1.2, 1.8), False),
            ("Frame 1 restores rule", lambda: hou.setFrame(1), (0.4, 0.8, 1.2), False),
            ("Camera navigation", lambda: pass_fixture.parm(f.camera, "tx").set(0.3), (0.4, 0.8, 1.2), False),
            ("Packed object transform", lambda: pass_fixture.parm(f.heavy, "tx").set(0.5), (0.4, 0.8, 1.2), False),
            ("Rename assigned material", lambda: self.mat.setName("renamed_live_stylesheet"), (0.4, 0.8, 1.2), False),
        ]
        self.report = dict(result="running", viewer="native" if self.native else "cycles", events=[])
        self.index = 0
        self.began = time.monotonic()
        self.busy = False
        self.finished = False
        self.phase = "mount"
        self.entry = self.session = self.panel = None
        self.due = self.began + 0.2
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(550, 420))
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
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(950, 700))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def repair_material(self):
        mat = ch.create_material(name="live_stylesheet_repair", shader="emission")
        mat.node("surface_shader").parmTuple("color").set((0.7, 0.15, 0.3))
        pass_fixture.parm(mat.node("surface_shader"), "strength").set(1)

    def animate(self):
        self.control.setExpression(
            'import json\nreturn json.dumps({"styles":[{"target":{},"overrides":{"materialParameters":{"power":hou.frame()+1}}}]})',
            hou.exprLanguage.Python,
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
                        and status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.callback)
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
            if time.monotonic() < self.due:
                return
            entry = registry.get("rop:" + str(self.f.rop.sessionId()))
            if entry is None:
                return
            self.entry = entry
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if (
                entry.change_serial != entry.applied_serial
                or entry.requested_serial
                or not info
                or not info["complete"]
                or info["generation"] != status["generation"]
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            label, _, expected, warning = self.steps[self.index]
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            frame = entry.session.frame()
            pixel = self.f.pixels(frame)[32, 32, :3]
            if not np.allclose(pixel, expected, atol=3e-5, rtol=3e-5):
                return
            assert bool(status["source_diagnostics"]) == warning, status
            if label in (
                "Uniform rule on heavy shared prototype",
                "Edit uniform heavy rule",
                "Broken heavy rule keeps geometry",
            ):
                refs = [
                    r for r in entry.session.native_material_references() if r["path"] == self.heavy_material.path()
                ]
                assert refs, entry.session.native_material_references()
                expected_value = (
                    0.3 if label.startswith("Uniform") else 0.6 if label.startswith("Edit") else "bad_value"
                )
                for ref in refs:
                    fields = {k: v for text in ref["overrides"] for k, v in ast.literal_eval(text).items()}
                    assert fields.get("roughness") == expected_value, fields
            if self.native:
                if not entry.helper_pid:
                    return
                try:
                    if not np.allclose(self.tab.pixel("C", 32, 32)[:3], expected, atol=3e-5, rtol=3e-5):
                        return
                except hou.OperationFailed:
                    return
            if self.report["events"]:
                first = self.report["events"][0]
                assert status["renderer_pid"] == first["status"]["renderer_pid"]
                assert self.heavy.cookCount() == first["heavy_cooks"], status
                if label in ("Camera navigation", "Packed object transform"):
                    assert (
                        status["geometry_conversions"] == self.report["events"][-1]["status"]["geometry_conversions"]
                    ), status
            assert len(registry.entries()) == 1 and status["packed_instances"] == 12, status
            self.report["events"].append(dict(label=label, status=status, heavy_cooks=self.heavy.cookCount()))
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                self.close()
                return
            self.steps[self.index][1]()
            self.due = time.monotonic() + 0.2
        except BaseException:
            self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        if self.phase == "closing":
            self.report.update(result="failed", error=error)
            self.finished = True
            self.save()
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
