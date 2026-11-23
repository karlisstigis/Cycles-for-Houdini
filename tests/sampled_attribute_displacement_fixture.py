# SPDX-License-Identifier: Apache-2.0
"""Native animated UV/Cd inputs with an analytic displacement-motion target."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
from temporal_displacement_fixture import Fixture as TemporalFixture

CASES = ("uv", "uvmap", "color", "color_node", "image_uv", "combined")


class Fixture:
    def __init__(self, device):
        self.__dict__.update(TemporalFixture(device).__dict__)
        pass_fixture.parm(self.rop, "pass_uv").set(0)  # UVs can then be genuinely unused in color-only cases.
        self.dest = artifacts_root() / "examples/sampled-attributes"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.uv = self.material.createNode("cycles::texture_coordinate::1", "sampled_uv")
        self.uvmap = self.material.createNode("cycles::uvmap::1", "sampled_uv_map")
        self.split = self.material.createNode("cycles::separate_xyz::1", "sampled_u")
        self.color = self.material.createNode("cycles::attribute::1", "sampled_color")
        pass_fixture.parm(self.color, "attribute").set("Cd")
        self.color_node = self.material.createNode("cycles::vertex_color::1", "sampled_color_attribute")
        pass_fixture.parm(self.color_node, "layer_name").set("Cd")
        self.combine = self.material.createNode("cycles::math::1", "combined_height")
        pass_fixture.parm(self.combine, "math_type").set("add")
        self.half = self.material.createNode("cycles::math::1", "average_height")
        pass_fixture.set_parameters(self.half, dict(math_type="multiply", value2=0.5))
        self.half.setNamedInput("value1", self.combine, "value")
        self.combine.setNamedInput("value1", self.split, "x")
        self.combine.setNamedInput("value2", self.color, "fac")
        self.gradient = self.dest / "height-gradient.exr"
        pixels = np.broadcast_to(((np.arange(1024, dtype=np.float32) + 0.5) / 1024)[None, :, None], (2, 1024, 3)).copy()
        output = oiio.ImageOutput.create(str(self.gradient))
        assert output.open(str(self.gradient), oiio.ImageSpec(1024, 2, 3, oiio.FLOAT))
        assert output.write_image(pixels)
        assert output.close()
        self.image.setInput(self.image.inputIndex("vector"), None)
        pass_fixture.set_parameters(
            self.image, dict(filename=str(self.gradient), interpolation="linear", extension="clamp")
        )
        self.offset = self.material.createNode("cycles::combine_xyz::1", "attribute_offset")
        self.vector.setNamedInput("vector", self.offset, "vector")
        pass_fixture.parm(self.vector, "space").set("object")
        self.missing = self.obj.createNode("attribdelete", "missing_shutter_attribute")
        self.missing.setInput(0, self.sop)
        self.invalid = self.obj.createNode("attribwrangle", "invalid_shutter_attribute")
        self.invalid.setInput(0, self.sop)
        self.switch = self.obj.createNode("switch", "OUT_SAMPLED_ATTRIBUTES")
        for i, node in enumerate((self.sop, self.missing, self.invalid)):
            self.switch.setInput(i, node)
        pass_fixture.parm(self.switch, "input").setExpression(
            'if($T<0,ch("%s"),0)' % self.parm.path(), hou.exprLanguage.Hscript
        )
        self.switch.setDisplayFlag(True)
        self.switch.setRenderFlag(True)
        group = self.source.parmTemplateGroup()
        parm = group.find("test_broken")
        parm.setLabel("Shutter Attribute: 0 Valid, 1 Missing, 2 Invalid")
        parm.setScriptCallback("")
        parm.setMaxValue(2)
        group.replace("test_broken", parm)
        self.source.setParmTemplateGroup(group)
        self.parm = pass_fixture.parm(self.source, "test_broken")
        self.configure(CASES[0])

    def configure(self, case):
        self.case = case
        self.edit(False)
        pass_fixture.parm(self.obj, "tx").set(-1.5 if case == "image_uv" else -0.5)
        rate = 24 if case == "image_uv" else 72
        pass_fixture.parm(self.sop, "snippet").set(
            "v@N=set(1,0,0); float height=.5+@Time*%d; v@uv=set(height,0.5,0); v@Cd=set(height,height,height);" % rate
        )
        pass_fixture.parm(self.scalar, "height").deleteAllKeyframes()
        pass_fixture.parm(self.scalar, "height").set(0.5)
        pass_fixture.parm(self.scalar, "scale").set(3 if case == "image_uv" else 1)
        pass_fixture.parm(self.vector, "scale").set(3 if case == "image_uv" else 1)
        self.split.setNamedInput("vector", self.uvmap if case == "uvmap" else self.uv, "UV")
        target, output = (
            (self.color, "fac")
            if case == "color"
            else (self.color_node, "color")
            if case == "color_node"
            else (self.image, "color")
            if case == "image_uv"
            else (self.half, "value")
            if case == "combined"
            else (self.split, "x")
        )
        self.scalar.setNamedInput("height", target, output)
        self.offset.setNamedInput("x", target, output)
        attribute = "Cd" if case in ("color", "color_node") else "uv"
        pass_fixture.parm(self.missing, "ptdel").set(attribute)
        pass_fixture.parm(self.invalid, "snippet").set('v@%s=set(atof("nan"),0,0);' % attribute)
        self.material.node("surface_output").setNamedInput("displacement", self.vector, "displacement")
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        pass_fixture.set_parameters(self.rop, dict(allowmotionblur=1, geometry_motion="deformation"))

    def edit(self, broken):
        self.parm.set(int(broken))

    def check(self, frame, status, broken, baseline=None):
        assert not status["error"] and status["state"] != "failed", status
        assert status["geometry_resources"] == status["object_resources"] == 1, status
        issues = status["source_diagnostics"]
        assert bool(issues) == bool(broken), (self.case, broken, issues)
        if broken:
            assert not any(d["code"].startswith("motion.displacement_") for d in issues), issues
            assert any(d["code"].startswith("geometry.shutter_") for d in issues), issues
        planes = {}
        for name, p in frame["planes"].items():
            values = np.frombuffer(p["pixels"], np.float32).reshape(90, 160, p["channels"])
            assert np.isfinite(values).all(), name
            planes[name] = values.copy()
        assert planes["combined"][:, :, :3].max() > 0.1, "Geometry disappeared"
        if baseline is not None:
            difference = float(np.mean(np.abs(planes["combined"] - baseline["combined"])))
            assert difference > 0.0001 if broken else difference < 1e-6, (self.case, broken, difference)
        return planes
