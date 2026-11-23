# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
from pathlib import Path
import os
from sampled_attribute_displacement_fixture import Fixture as Base

CASES = ("scalar", "integer", "vector", "float2", "color_layer", "uv_map", "combined")


class Fixture:
    def __init__(self, device):
        self.__dict__.update(Base(device).__dict__)
        self.dest = artifacts_root() / "examples/named-field-displacement"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.named_scalar = self.material.createNode("cycles::attribute::1", "named_scalar")
        self.named_scalar.parm("attribute").set("height_scalar")
        self.normalize = self.material.createNode("cycles::math::1", "integer_to_height")
        self.normalize.setParms(dict(math_type="multiply", value2=0.125))
        self.configure("scalar")

    def configure(self, case):
        Base.configure(
            self, "color_node" if case == "color_layer" else "uvmap" if case in ("uv_map", "float2") else "color"
        )
        self.case = case
        self.sop.parm("snippet").set(
            "v@N=set(1,0,0); float h=.5+@Time*72; f@height_scalar=h; i@height_id=int(floor(h*8+.5)); v@height_vector=set(h,h,h); v@height_uv=set(h,.5,0); u@height_uv2=set(h,0);"
        )
        self.color.parm("attribute").set("height_vector")
        self.color_node.parm("layer_name").set("height_vector")
        self.uvmap.parm("attribute").set("height_uv")
        self.field = dict(
            scalar="height_scalar",
            integer="height_id",
            vector="height_vector",
            float2="height_uv2",
            color_layer="height_vector",
            uv_map="height_uv",
            combined="height_scalar",
        )[case]
        if case == "scalar":
            node, output = self.named_scalar, "fac"
        elif case == "integer":
            self.color.parm("attribute").set("height_id")
            self.normalize.setNamedInput("value1", self.color, "fac")
            node, output = self.normalize, "value"
        elif case == "vector":
            node, output = self.color, "fac"
        elif case == "float2":
            self.color.parm("attribute").set("height_uv2")
            self.split.setNamedInput("vector", self.color, "vector")
            node, output = self.split, "x"
        elif case == "color_layer":
            node, output = self.color_node, "color"
        elif case == "uv_map":
            self.split.setNamedInput("vector", self.uvmap, "UV")
            node, output = self.split, "x"
        else:
            self.combine.setNamedInput("value1", self.named_scalar, "fac")
            self.combine.setNamedInput("value2", self.color, "fac")
            node, output = self.half, "value"
        self.offset.setNamedInput("x", node, output)
        self.missing.parm("ptdel").set(self.field)
        self.invalid.setInput(0, self.missing)
        self.invalid.parm("snippet").set(
            ("v@%s=set(0,0,0);" if case in ("scalar", "integer", "float2", "combined") else "f@%s=0;") % self.field
        )
        self.material.layoutChildren()

    def edit(self, value):
        Base.edit(self, value)

    def check(self, *args, **kwargs):
        return Base.check(self, *args, **kwargs)
