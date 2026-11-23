# SPDX-License-Identifier: Apache-2.0
"""Requested-frame authored normals with independent linear velocity motion."""

from subdivision_test_setup import configure_render, set_subdivision
import hou, numpy as np
from temporal_displacement_fixture import Fixture as TemporalFixture


class Fixture(TemporalFixture):
    def __init__(self, device):
        super().__init__(device)
        self.vertex = self.obj.createNode("attribwrangle", "authored_vertex_normals")
        self.vertex.setInput(0, self.sop)
        self.vertex.parm("class").set(3)
        self.vertex.setDisplayFlag(True)
        self.vertex.setRenderFlag(True)
        configure_render(
            self.rop,
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                geometry_motion="velocity",
                use_material_assignments=1,
                materialpath="",
            ),
        )
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.setup()

    def setup(self, displaced=False, vertex=False, broken=False):
        self.displaced = displaced
        self.vertex_owner = vertex
        self.broken = broken
        normal = (1, 0, 0) if displaced else (0.6, 0, 0.8)
        self.normal = normal
        self.obj.parm("tx").set(-0.5 if displaced else 0)
        self.sop.parm("snippet").set(
            "v@v=set(%s,0,0);v@N=set(%s);" % (48 if displaced else 72, ",".join(map(str, normal)))
        )
        self.vertex.bypass(not vertex)
        self.vertex.parm("snippet").set("v@N=set(%s);" % ("0,0,0" if broken else ",".join(map(str, normal))))
        self.material.parm("cycles_displacement_method").set("true" if displaced else "bump")
        self.scalar.parm("height").setExpression(".5+$T*24", hou.exprLanguage.Hscript)
        self.material.node("surface_output").setNamedInput("displacement", self.scalar, "displacement")
        configure_render(
            self.rop,
            dict(allowmotionblur=1, geometry_motion="velocity", shutteroffset=0, subdivision=dict(type="none")),
        )
        self.camera.parm("tx").set(0)

    @staticmethod
    def pixels(frame):
        return {
            name: np.frombuffer(plane["pixels"], np.float32)
            .reshape(frame["height"], frame["width"], plane["channels"])
            .copy()
            for name, plane in frame["planes"].items()
        }
