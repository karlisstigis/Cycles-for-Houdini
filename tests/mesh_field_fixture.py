# SPDX-License-Identifier: Apache-2.0
"""One million-triangle packed prototype with independent native shader fields."""

import pass_fixtures as pass_fixture

import numpy as np
from stylesheet_storage_fixture import Fixture as BaseFixture


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


class Fixture(BaseFixture):
    def __init__(self, device="CPU", heavy=True):
        super().__init__(device, heavy=heavy)
        seed = self.obj.createNode("attribwrangle", "prototype_fields")
        seed.setInput(0, self.prototype)
        pass_fixture.parm(seed, "snippet").set("v@uv={.2,.4,0}; v@Cd={.2,.4,.6}; v@paint={.1,.05,.02}; f@mask=.03;")
        self.edits = {}
        previous = seed
        for name in ("uv", "Cd", "paint", "mask"):
            node = self.obj.createNode("attribwrangle", "edit_" + name)
            node.setInput(0, previous)
            self.edits[name] = node
            previous = node
        self.output = previous
        self.copies.setInput(0, previous)
        sources = {}
        for name in ("Cd", "paint", "mask"):
            node = self.material.createNode("cycles::attribute::1", "read_" + name)
            pass_fixture.parm(node, "attribute").set(name)
            sources[name] = node
        add = self.material.createNode("cycles::vector_math::1", "add_color")
        pass_fixture.parm(add, "math_type").set("add")
        add.setNamedInput("vector1", sources["Cd"], "color")
        add.setNamedInput("vector2", sources["paint"], "color")
        sum_ = self.material.createNode("cycles::vector_math::1", "add_mask")
        pass_fixture.parm(sum_, "math_type").set("add")
        sum_.setNamedInput("vector1", add, "vector")
        sum_.setNamedInput("vector2", sources["mask"], "fac")
        self.material.node("surface_shader").setNamedInput("color", sum_, "vector")
        pass_fixture.set_parameters(self.rop, dict(pass_position=1, pass_normal=1, pass_uv=1, pass_depth=1))
        self.offsets = {name: 0 for name in self.edits}
        self.broken = None

    def edit(self, name, code, offset=0, broken=False):
        pass_fixture.parm(self.edits[name], "snippet").set(code)
        self.offsets[name] = offset
        self.broken = name if broken else None

    def check(self, frame, offset=0):
        pixels = plane(frame, "combined")
        mask = (pixels[:, :, 3] > 0.999) & (plane(frame, "depth")[:, :, 0] < 100)
        assert mask.sum() > 500
        expected = (
            np.array((0.33, 0.48, 0.65))
            + self.offsets["mask"]
            + np.array((0, self.offsets["Cd"], self.offsets["paint"]))
        )
        if self.broken in ("Cd", "paint", "mask"):
            expected = np.array((1, 0, 1))
        np.testing.assert_allclose(
            pixels[:, :, :3][mask], np.broadcast_to(expected, (mask.sum(), 3)), rtol=0, atol=3e-5
        )
        uv = plane(frame, "uv")[:, :, :2][mask]
        expected_uv = np.array((0.2 + self.offsets["uv"], 0.4)) if self.broken != "uv" else np.zeros(2)
        np.testing.assert_allclose(uv, np.broadcast_to(expected_uv, uv.shape), rtol=0, atol=3e-5)
        np.testing.assert_allclose(plane(frame, "position")[:, :, 2][mask], 0, rtol=0, atol=3e-5)
        return [expected.tolist()]
