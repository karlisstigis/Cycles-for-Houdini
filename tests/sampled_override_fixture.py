# SPDX-License-Identifier: Apache-2.0
"""Native Material SOP animation on the analytic temporal-displacement fixture."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
import os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from temporal_displacement_fixture import Fixture as TemporalFixture
from light_shader_fixture import write_image


class Fixture(TemporalFixture):
    def __init__(self, device):
        super().__init__(device)
        self.dest = artifacts_root() / "examples/sampled-overrides"
        self.dest.mkdir(parents=True, exist_ok=True)
        ch.promote_parameter(self.material, self.scalar, "height", name="height")
        ch.promote_parameter(self.material, self.image, "filename", name="image_file")
        ch.promote_parameter(self.material, self.source, "strength", name="power")
        self.material.parm("height").deleteAllKeyframes()
        self.material.parm("height").set(0.5)
        self.material.parm("power").set(1)
        self.files = [self.dest / f"override-height-{index}.exr" for index in range(5)]
        for index, path in enumerate(self.files):
            write_image(path, (0.25 + index * 0.125,) * 3)
        self.material.parm("image_file").deleteAllKeyframes()
        self.material.parm("image_file").set(str(self.files[2]))
        configure_render(self.rop, dict(materialpath="", use_material_assignments=1, geometry_motion="deformation"))
        self.assigned = self.obj.createNode("material", "animated_native_override")
        self.assigned.setInput(0, self.sop)
        self.assigned.setParms(
            dict(shop_materialpath1=self.material.path(), num_local1=1, local1_name1="height", local1_type1="float")
        )
        self.assigned.addSpareParmTuple(hou.FloatParmTemplate("test_speed", "Override Speed", 1, default_value=(72,)))
        self.output = self.obj.createNode("attribwrangle", "material_source_repair")
        self.output.setInput(0, self.assigned)
        self.output.parm("class").set(1)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.setup("parameter")

    def setup(self, case):
        self.case = case
        self.output.parm("snippet").set("")
        self.assigned.parm("num_local1").set(1)
        self.obj.parm("shop_materialpath").set(self.material.path())
        self.assigned.parm("shop_materialpath1").set(self.material.path())
        self.material.parm("height").deleteAllKeyframes()
        self.material.parm("height").set(0.5)
        self.sop.parm("snippet").set(
            "v@N=set(1,0,0);" + ("@P.x+=@Time*48;" if case in ("deformation", "image") else "")
        )
        self.scalar.setInput(self.scalar.inputIndex("height"), None)
        self.material.node("surface_output").setNamedInput("displacement", self.scalar, "displacement")
        self.scalar.parm("midlevel").deleteAllKeyframes()
        self.scalar.parm("midlevel").set(0)
        self.assigned.parm("local1_fval1").deleteAllKeyframes()
        self.assigned.parm("local1_sval1").deleteAllKeyframes()
        self.assigned.parm("test_speed").set(24 if case == "deformation" else 36 if case == "base_animation" else 72)
        self.assigned.setParms(dict(num_local1=1, local1_name1="height", local1_type1="float"))
        self.assigned.parm("local1_fval1").setExpression('.5+$T*ch("test_speed")', hou.exprLanguage.Hscript)
        if case == "base_animation":
            self.scalar.parm("midlevel").setExpression("-$T*36", hou.exprLanguage.Hscript)
        if case == "image":
            self.scalar.setNamedInput("height", self.image, "color")
            self.assigned.setParms(dict(local1_name1="image_file", local1_type1="string"))
            pattern = str(self.dest / "override-height-%d.exr").replace("\\", "/")
            self.assigned.parm("local1_sval1").setExpression(
                repr(pattern) + " % int(round((hou.time()+1.0/96)*192))", hou.exprLanguage.Python
            )
        self.material.parm("cycles_displacement_method").set("true")
        configure_render(self.rop, dict(allowmotionblur=1, shutteroffset=0, subdivision=dict(type="none")))
        hou.node(self.rop.evalParm("camera")).parm("tx").set(0)

    @staticmethod
    def pixels(frame):
        return {
            name: np.frombuffer(plane["pixels"], np.float32)
            .reshape(frame["height"], frame["width"], plane["channels"])
            .copy()
            for name, plane in frame["planes"].items()
        }
