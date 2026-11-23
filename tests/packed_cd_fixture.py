# SPDX-License-Identifier: Apache-2.0
"""Cd on Houdini geometry domains and uniform packed-instance domains."""

import numpy as np
from packed_instance_attribute_fixture import Fixture as PackedFixture


class Fixture(PackedFixture):
    def __init__(self, device="CPU"):
        super().__init__(device)
        self.dest = self.dest.parent / "packed-cd"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.rand.parm("attribute").set("Cd")
        self.color_node = self.material.createNode("cycles::vertex_color::1", "color_attribute")
        self.source = self.rand
        self.emission.setNamedInput("color", self.source, "color")
        self.set_cd()

    def select_source(self, color_attribute=False, unnamed=False):
        self.source = self.color_node if color_attribute else self.rand
        if color_attribute:
            self.color_node.parm("layer_name").set("" if unnamed else "Cd")
        self.emission.setNamedInput("color", self.source, "color")

    def set_cd(self, mode="good", owner="primitive", offset=0, packed=True):
        self.copies.parm("pack").set(int(packed))
        self.fields.parm("class").set(self.fields.parm("class").menuItems().index(owner))
        self.mode, self.offset, self.owner, self.packed = mode, offset, owner, packed
        source = dict(primitive="@primnum", point="@ptnum", vertex="@vtxnum", detail="0")[owner]
        divisor = 1 if packed or owner in ("primitive", "detail") else 4
        code = "int k=int(" + source + ")/" + str(divisor) + ";"
        if mode == "bad_type":
            code += 's@Cd="unusable color";'
        elif mode == "bad_size":
            code += "f@Cd=.4;"
        elif mode != "missing":
            code += "v@Cd=set(.2+.2*k+" + str(offset) + ",.1*(k+1),.3);"
            if mode == "bad_one":
                code += 'if(k==1)v@Cd.y=atof("nan");'
            elif mode == "animation":
                code = (
                    "int k=int("
                    + source
                    + ');v@Cd=set(.1*@Frame+.1*k,.1*(k+1),.3);if(@Frame==3 && k==1)v@Cd.y=atof("nan");'
                )
        self.fields.parm("snippet").set(code)

    def expected(self):
        if self.mode in ("bad_type", "bad_size", "missing"):
            return [(1, 0, 1)]
        values = [(0.2 + 0.2 * k + self.offset, 0.1 * (k + 1), 0.3) for k in range(1 if self.owner == "detail" else 4)]
        if self.mode == "bad_one":
            values[1] = (1, 0, 1)
        return values

    def colors(self, frame, expected=None, warning=False):
        return super().colors(frame, self.expected() if expected is None else expected, warning)

    def heights(self, frame, colors, scale=0.1, disabled=False, independent=False):
        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])

        beauty, position = plane("combined"), plane("position")
        normal = float(self.grid.geometry().prims()[0].normal()[2])
        for color in colors:
            mask = (beauty[:, :, 3] > 0.999) & np.all(np.isclose(beauty[:, :, :3], color, atol=3e-4, rtol=0), axis=2)
            assert mask.sum() > 4, color
            height = 0.15 if independent else 0 if disabled or tuple(color) == (1, 0, 1) else color[0] * scale
            np.testing.assert_allclose(position[:, :, 2][mask], normal * height, atol=2e-4, rtol=0)
