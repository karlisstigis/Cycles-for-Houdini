# SPDX-License-Identifier: Apache-2.0
"""An independently translated mesh with repairable shutter coordinate fields."""

from subdivision_test_setup import configure_render, set_subdivision
import hou
import numpy as np
from sampled_attribute_displacement_fixture import Fixture as SampledFixture
from independent_binding_fixture import motion_pixels

CASES = ("uv", "uvmap", "color", "color_node", "named", "named_uv", "named_color", "image_uv")


class Fixture(SampledFixture):
    def __init__(self, device):
        self.initializing = True
        super().__init__(device)
        self.initializing = False
        self.field = self.material.createNode("cycles::attribute::1", "recovery_height")
        self.field.parm("attribute").set("height_field")
        self.named_uv = self.material.createNode("cycles::uvmap::1", "recovery_uv")
        self.named_uv.parm("attribute").set("map1")
        self.named_color = self.material.createNode("cycles::vertex_color::1", "recovery_color")
        self.named_color.parm("layer_name").set("tint")
        self.configure("uv")

    def configure(self, case, *, nominal=False, scale_animation=False):
        if self.initializing:
            return super().configure(case)
        super().configure(case if case in ("uv", "uvmap", "color", "color_node", "image_uv") else "uv")
        self.case = case
        self.nominal_failure = nominal
        self.scale_animation = scale_animation
        self.obj.parm("tx").deleteAllKeyframes()
        self.obj.parm("tx").set(-0.5)
        self.sop.parm("snippet").set(
            "v@N=set(1,0,0);@P.x+=@Time*48;float h=.5+@Time*24;v@uv=set(h,.5,0);v@Cd=set(h,h,h);f@height_field=h;v@map1=set(h,.5,0);v@tint=set(h,h,h);"
        )
        self.scalar.parm("scale").deleteAllKeyframes()
        self.scalar.parm("scale").set(1)
        self.vector.parm("scale").deleteAllKeyframes()
        self.vector.parm("scale").set(1)
        if scale_animation:
            self.vector.parm("scale").setExpression("1+$T*48", hou.exprLanguage.Hscript)
        if case == "named":
            self.offset.setNamedInput("x", self.field, "fac")
        elif case == "named_uv":
            self.split.setNamedInput("vector", self.named_uv, "UV")
            self.offset.setNamedInput("x", self.split, "x")
        elif case == "named_color":
            self.offset.setNamedInput("x", self.named_color, "color")
        self.attribute_name = (
            "Cd"
            if case in ("color", "color_node")
            else "height_field"
            if case == "named"
            else "map1"
            if case == "named_uv"
            else "tint"
            if case == "named_color"
            else "uv"
        )
        self.missing.parm("ptdel").set(self.attribute_name)
        self.invalid.parm("snippet").set(
            ("f@" if case == "named" else "v@")
            + self.attribute_name
            + "="
            + ('atof("nan");' if case == "named" else 'set(atof("nan"),0,0);')
        )
        condition = "abs($T)<1e-8" if nominal else "abs($T)>1e-8"
        self.switch.parm("input").setExpression(
            "if(" + condition + ',ch("' + self.parm.path() + '"),0)', hou.exprLanguage.Hscript
        )
        configure_render(self.rop, dict(shutteroffset=0, subdivision=dict(type="none")))

    def expected_pixels(self, broken, samples=3, offset=0):
        times = np.linspace((offset - 1) / 96, (offset + 1) / 96, samples)
        height = (
            np.zeros(samples)
            if broken and self.nominal_failure
            else np.full(samples, 0.5)
            if broken
            else 0.5 + times * 24
        )
        scale = 1 + times * 48 if self.scale_animation else np.ones(samples)
        camera = hou.node(self.rop.evalParm("camera"))
        shift = self.obj.evalParm("tx") + 0.5 - camera.evalParm("tx")
        return motion_pixels(times * 48 + height * scale - 0.5 + shift)
