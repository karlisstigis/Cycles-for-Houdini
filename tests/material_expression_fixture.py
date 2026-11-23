# SPDX-License-Identifier: Apache-2.0
"""Native Local Overrides and stylesheet categories on contributing HScript."""

import json
from pathlib import Path
import hou
import numpy as np
from material_ramp_override_fixture import Fixture as RampFixture, ROOT

REFERENCE = json.loads((ROOT / "tests/fixtures/houdini-material-expressions.json").read_text())
COLOR = np.asarray((0.1, 0.2, 0.4))


class Fixture(RampFixture):
    def __init__(self, device, *, heavy=False):
        super().__init__(device, heavy=heavy)
        self.light.parm("light_intensity").set(0)
        self.material.addSpareParmTuple(hou.FloatParmTemplate("gain", "Gain", 1, default_value=(0.2,)))
        self.driver = self.material.createNode("cycles::value::1", "numeric_expression")
        self.emission.setInput(self.emission.inputIndex("color"), None)
        self.emission.parmTuple("color").set(tuple(COLOR))
        self.emission.setNamedInput("strength", self.driver, "value")
        self.offset_component = tuple(self.material.parmTuple("offset"))[2].name()
        self.shared = self.material.createNode("cycles::vector_displacement::1", "scoped_expression_height")
        # The default this test was written for (new nodes now take Blender's).
        self.shared.setParms(dict(space="object"))
        self.shared.parmTuple("vector").set((0, 0, 1))
        self.shared.parm("scale").setExpression('fit(ch("../gain"),0,1,.1,.3)', hou.exprLanguage.Hscript)
        self.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Stylesheet", 1))
        self.stylesheet = self.obj.parm("shop_materialstylesheet")
        self.source_path = self.scene.path() + "/expression_controls"
        self.source = None
        self.source_high = 0.8
        self.make_source()
        self.expression('fit(ch("../gain"),0,1,1,5)')
        self.native()

    @staticmethod
    def rgb(power):
        return COLOR * power

    def expression(self, text):
        self.active_expression = text
        self.driver.parm("value").setExpression(text, hou.exprLanguage.Hscript)

    def native(self, gain=0.35, height=0.23):
        self.stylesheet.set("")
        self.constant_height()
        self.set_fields([("gain", "float", gain), (self.offset_component, "float", height)])
        self.gain = gain
        self.height = height

    def constant_height(self):
        self.material.node("surface_output").setNamedInput("displacement", self.offset, "displacement")

    def style(self, material=None, surface=None, displacement=None):
        self.set_fields([])
        self.material.node("surface_output").setNamedInput("displacement", self.shared, "displacement")
        fields = {
            name: values
            for name, values in (
                ("materialParameters", material),
                ("surfaceParameters", surface),
                ("displaceParameters", displacement),
            )
            if values is not None
        }
        self.stylesheet.set(json.dumps({"styles": [{"target": {}, "overrides": fields}]}))

    def make_source(self):
        assert hou.node(self.source_path) is None
        self.source = self.scene.createNode("null", "expression_controls")
        assert self.source.path() == self.source_path
        self.source.addSpareParmTuple(hou.FloatParmTemplate("sampled", "Sampled", 1))
        self.edit_source(self.source_high)

    def edit_source(self, high):
        self.source_high = high
        self.source.parm("sampled").setExpression("if($FF<5,.2," + repr(high) + ")", hou.exprLanguage.Hscript)

    def delete_source(self):
        self.source.destroy()
        self.source = None

    def sampled(self, kind="chf", relative=False):
        sample = "$FF+7" if kind == "chf" and relative else "$T+7/24" if relative else "8" if kind == "chf" else "7/24"
        self.expression('ch("../gain")+' + kind + "(" + repr(self.source_path + "/sampled") + "," + sample + ")")
        return self.rgb(self.gain + self.source_high)

    def scoped(self, surface=0.6, displacement=0.9):
        self.expression('fit(ch("../gain"),0,1,1,5)')
        self.style(material={"gain": 0.3}, surface={"gain": surface}, displacement={"gain": displacement})
        return self.rgb(1 + surface * 4), 0.1 + displacement * 0.2

    def snapshot(self):
        # rawValue only: keyframe/evaluation APIs can register channel links and
        # conceal first-active-capture dependency failures.
        return self.authored()

    def integer_control_fault(self, broken):
        self.material.parm("cycles_pass_index").setExpression(
            'int((ch("gain")-.2)*1e30)' if broken else 'int(ch("gain"))', hou.exprLanguage.Hscript
        )
        return self.rgb(1.8 if broken else 4.2)

    def reference(self, identifier):
        return next(
            row
            for row in REFERENCE["cases"]
            if row["id"] == identifier and row["fps"] == 24 and row["frame"] in (1.25, 0.5)
        )
