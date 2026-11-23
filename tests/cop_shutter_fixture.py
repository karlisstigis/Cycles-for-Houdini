# SPDX-License-Identifier: Apache-2.0
"""COP height samples on the independently specified temporal-displacement scene."""

from support.paths import artifacts_root
from pathlib import Path
import os
import hou
from temporal_displacement_fixture import Fixture as TemporalFixture


class Fixture(TemporalFixture):
    def __init__(self, device):
        super().__init__(device)
        self.dest = artifacts_root() / "stage3/textures/cop-shutter"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.legacy = hou.node("/img").createNode("img", "shutter_cops")
        self.height = self.legacy.createNode("color", "height")
        self.height.setParms(dict(overridesize=1, size1=16, size2=8, depth=4))
        self.height.setParms(dict(overriderange=1, start=-100, length=240))
        self.height.parm("colora").set(1)
        self.modern = hou.node("/img").createNode("copnet", "shutter_copernicus")
        self.modern.setParms(dict(setres=1, res1=16, res2=8, setprecision=1, precision=1))
        self.constant = self.modern.createNode("constant", "height")
        self.constant.parm("signature").set("f1")
        self.color_cop = self.legacy.createNode("color", "surface_color")
        self.color_cop.setParms(dict(overridesize=1, size1=16, size2=8, depth=4))
        self.color_cop.parmTuple("color").set((0.2, 0.4, 0.8, 1))
        self.color_image = self.material.createNode("cycles::image_texture::1", "independent_cop_color")
        self.color_image.setParms(dict(colorspace="data", alpha_type="ignore"))
        self.color_image.setNamedInput("vector", self.geometry, "position")
        self.source.setNamedInput("color", self.color_image, "color")
        self.source.addSpareParmTuple(hou.IntParmTemplate("cop_problem", "COP Problem", 1, default_value=(0,)))
        self.problem = self.source.parm("cop_problem")
        self.color_image.parm("filename").setExpression(
            'ifs(ch("'
            + self.problem.path()
            + '") == 2,"op:/img/missing_shutter_color/source","op:'
            + self.color_cop.path()
            + '")',
            hou.exprLanguage.Hscript,
        )
        self.rop.setParms(
            dict(samples=512, ipr_samples=512, render_threads=2, use_texture_cache=0, world_source="color")
        )
        self.choose("legacy")

    def choose(self, kind, speed=72):
        self.kind = kind
        # Legacy COP2 resolves integer image frames. Explicit pinned-frame
        # sequencing provides a changing legacy shutter input; a separate check
        # verifies its ordinary held-frame behavior. Copernicus uses fractions.
        for name in ("colorr", "colorg", "colorb"):
            self.height.parm(name).setExpression(".5+($F-2)*.75", hou.exprLanguage.Hscript)
        self.constant.parm("f1").setExpression(".5+$T*" + str(speed), hou.exprLanguage.Hscript)
        self.scalar.setNamedInput("height", self.image, "color")
        path = '"op:' + self.constant.path() + '"'
        if kind == "legacy":
            path = (
                'ifs($T < 0,"op:'
                + self.height.path()
                + '[1]",ifs($T > 0,"op:'
                + self.height.path()
                + '[3]","op:'
                + self.height.path()
                + '[2]"))'
            )
        self.image.parm("filename").setExpression(
            'ifs(ch("'
            + self.problem.path()
            + '") == 1 && $T < 0,"op:/img/missing_shutter_height/source",'
            + path
            + ")",
            hou.exprLanguage.Hscript,
        )
        self.problem.set(0)

    def break_input(self, value):
        self.problem.set(value)
