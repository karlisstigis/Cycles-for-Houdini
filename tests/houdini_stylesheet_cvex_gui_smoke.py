# SPDX-License-Identifier: Apache-2.0
"""CVEX source and data repairs in one viewer retain a heavy shared prototype."""

import ast, json, subprocess
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from houdini_material_stylesheet_gui_smoke import Workflow as Base, sheet


def script(code, bindings=None, returned="result"):
    value = {"code": code, "return": returned}
    if bindings is not None:
        value["bindings"] = bindings
    return {"script": value}


class Workflow(Base):
    def __init__(self):
        super().__init__()
        f = self.f
        self.dest = f.dest.parent / "stylesheet-cvex-live"
        self.dest.mkdir(exist_ok=True)
        self.source = f.obj.createNode("python", "script_binding_data")
        self.source.setInput(0, f.sop)
        self.source.addSpareParmTuple(hou.IntParmTemplate("data_kind", "Data Kind", 1, default_value=(1,)))
        self.source.parm("python").set(
            "g=hou.pwd().geometry();kind=hou.pwd().evalParm('data_kind')\nif kind:\n a=g.addAttrib(hou.attribType.Prim,'gain',(0.,0.,0.) if kind==3 else 0.)\n for p in g.prims():p.setAttribValue(a,(2.,3.,4.) if kind==3 else float('nan') if kind==2 else 2.)"
        )
        self.source.setDisplayFlag(True)
        self.source.setRenderFlag(True)
        self.code = "cvex lookup(float gain=1; export float result=0){result=gain*2;}"
        self.bound = script(self.code, {"gain": "gain"})
        self.file = self.dest / "live_program.vex"
        self.vfl = self.dest / "live_program.vfl"
        self.compile_program(3)
        self.file_script = {"script": {"command": '"' + self.file.as_posix() + '"', "return": "result"}}
        self.make_node()
        self.node_script = {"script": {"node": self.shop.path(), "return": "result"}}
        self.height = self.mat.createNode("cycles::displacement::1", "live_script_height")
        self.height.setParms(dict(height=0.15, scale=1, midlevel=0, space="object"))
        self.mat.parm("cycles_displacement_method").set("true")
        ch.promote_parameter(self.mat, self.height, "height", name="height")
        self.height_script = script(
            "cvex height(float gain=1; export float result=0){result=gain*.1;}", {"gain": "gain"}
        )
        self.steps = [
            ("Underlying material", lambda: None, 1, False),
            ("Bind native primitive data", lambda: self.apply({"power": self.bound}), 4, False),
            ("Remove required data", lambda: self.source.parm("data_kind").set(0), 1, True),
            ("Repair required data", lambda: self.source.parm("data_kind").set(1), 4, False),
            ("Non-finite input", lambda: self.source.parm("data_kind").set(2), 1, True),
            ("Repair finite input", lambda: self.source.parm("data_kind").set(1), 4, False),
            ("Wrong tuple type", lambda: self.source.parm("data_kind").set(3), 1, True),
            ("Repair tuple type", lambda: self.source.parm("data_kind").set(1), 4, False),
            (
                "Compilation failure",
                lambda: self.apply({"power": script("cvex broken export float result=0){}")}),
                1,
                True,
            ),
            ("Repair code", lambda: self.apply({"power": self.bound}), 4, False),
            (
                "Overflow output",
                lambda: self.apply({"power": script("cvex overflow(export float result=0){result=exp(1000.);}")}),
                1,
                True,
            ),
            ("Repair output", lambda: self.apply({"power": self.bound}), 4, False),
            ("Lower native rule survives", self.lower_rule, 3, True),
            ("Remove bad rule", lambda: self.apply({"power": 2}), 2, False),
            ("External compiled program", lambda: self.apply({"power": self.file_script}), 3, False),
            ("Edit compiled program", lambda: self.compile_program(4), 4, False),
            ("Delete compiled program", lambda: self.file.unlink(), 1, True),
            ("Recreate compiled program", lambda: self.compile_program(2), 2, False),
            ("Corrupt compiled program", lambda: self.file.write_bytes(b"invalid compiled program"), 1, True),
            ("Repair compiled program", lambda: self.compile_program(3), 3, False),
            ("Native CVEX node program", lambda: self.apply({"power": self.node_script}), 3, False),
            ("Edit CVEX node parameter", lambda: self.export.parm("floatdef").set(4), 4, False),
            ("Break CVEX export", lambda: self.export.parm("parmname").set("other"), 1, True),
            ("Repair CVEX export", lambda: self.export.parm("parmname").set("result"), 4, False),
            ("Delete CVEX node", lambda: self.shop.destroy(), 1, True),
            ("Recreate CVEX node", self.make_node, 3, False),
            ("Enable valid scripted height", self.enable_height, 2, False),
            ("Break height input", lambda: self.source.parm("data_kind").set(2), 2, True),
            ("Repair height input", lambda: self.source.parm("data_kind").set(1), 2, False),
            (
                "Bad color preserves height",
                lambda: self.apply(
                    {"power": 2, "height": self.height_script, "tint": script("cvex broken export float result=0){}")}
                ),
                2,
                True,
            ),
            ("Remove height rule", self.remove_height, 1, False),
            ("Heavy script value", lambda: self.heavy_rule(0.3), 1, False),
            ("Edit heavy script value", lambda: self.heavy_rule(0.6), 1, False),
            ("Broken heavy script", lambda: self.heavy_rule(None), 1, True),
            ("Repair heavy script", lambda: self.heavy_style.set(""), 1, False),
            ("Camera navigation", lambda: f.camera.parm("tx").set(0.3), 1, False),
            ("Packed object transform", lambda: f.heavy.parm("tx").set(0.5), 1, False),
        ]
        self.steps = [
            (label, action, tuple(np.array((0.2, 0.4, 0.6)) * power), warning)
            for label, action, power, warning in self.steps
        ]
        self.report["scope"] = (
            "CVEX compilation/binding/output and file/node source repair; actual displacement; one viewer/worker; 1m shared prototype remains at one cook"
        )

    def apply(self, fields):
        self.control.set(sheet({"materialParameters": fields}))

    def compile_program(self, power):
        self.vfl.write_text("cvex live_program(export float result=0){result=" + str(power) + ";}")
        result = subprocess.run(
            [str(Path(hou.getenv("HFS")) / "bin/vcc.exe"), "-o", str(self.file), str(self.vfl)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    def make_node(self):
        self.shop = hou.node("/shop").createNode("vopcvex", "live_style_program")
        self.export = self.shop.createNode("parameter", "style_result")
        self.export.setParms(dict(parmname="result", exportparm=1, floatdef=3))

    def lower_rule(self):
        self.control.set(
            json.dumps(
                {
                    "styles": [
                        {"overrides": {"materialParameters": {"power": script(self.code, {"gain": "missing"})}}},
                        {"overrides": {"materialParameters": {"power": 3}}},
                    ]
                }
            )
        )

    def enable_height(self):
        self.mat.node("surface_output").setNamedInput("displacement", self.height, "displacement")
        self.apply({"power": 2, "height": self.height_script})

    def remove_height(self):
        output = self.mat.node("surface_output")
        output.setInput(output.inputIndex("displacement"), None)
        self.control.set("")

    def heavy_rule(self, value):
        code = (
            "cvex broken export float result=0){}"
            if value is None
            else "cvex roughness(export float result=0){result=" + str(value) + ";}"
        )
        self.heavy_style.set(sheet({"materialParameters": {"roughness": script(code)}}, dict(type="self")))

    def save(self):
        if self.report["events"] and self.phase != "closing":
            event = self.report["events"][-1]
            label = event["label"]
            if label in (
                "Enable valid scripted height",
                "Break height input",
                "Repair height input",
                "Bad color preserves height",
            ):
                expected = -0.15 if label == "Break height input" else -0.2
                np.testing.assert_allclose(
                    self.f.pixels(self.entry.session.frame(), "position")[32, 32, 2], expected, atol=2e-5
                )
            if label in ("Heavy script value", "Edit heavy script value"):
                expected = 0.3 if label == "Heavy script value" else 0.6
                refs = [
                    r
                    for r in self.entry.session.native_material_references()
                    if r["path"] == self.heavy_material.path()
                ]
                assert refs
                for ref in refs:
                    values = {key: value for text in ref["overrides"] for key, value in ast.literal_eval(text).items()}
                    np.testing.assert_allclose(values["roughness"], expected, atol=1e-6)
        super().save()


if __name__ == "__main__":
    workflow = Workflow()
