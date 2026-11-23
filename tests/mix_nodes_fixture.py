# SPDX-License-Identifier: Apache-2.0
"""Blender's single modern Mix interface with independent pixel references."""

import hou
import numpy as np
from cycles_houdini import _native
from shader_bypass_preview_fixture import Fixture as ShaderFixture

KINDS = ("mix_closure", "color", "float", "vector", "vector_non_uniform")
PRIVATE_KINDS = ("mix", "mix_float", "mix_vector", "mix_vector_non_uniform")
A = np.array((0.2, 0.4, 0.8))
B = np.array((0.6, 0.8, 1.2))


def check_layout(node, check_color=True):
    shader = node.type().name() == "cycles::mix_closure::1"
    if shader:
        data = ("closure1", "closure2")
        factor = "fac"
    else:
        assert node.type().name() == "cycles::mix_color::1" and node.type().description() == "Cycles Mix"
        kind = node.evalParm("data_type")
        non_uniform = kind == "vector" and node.evalParm("factor_mode") == "non_uniform"
        data = ("a", "b")
        factor = "fac"
        pane_data = (
            ("a_float", "b_float") if kind == "float" else ("a_vector", "b_vector") if kind == "vector" else ("a", "b")
        )
        pane_factor = "fac_vector" if non_uniform else "fac"
        node.updateParmStates()
        assert node.parm("factor_mode").isHidden() == (kind != "vector")
        assert node.parm("blend_type").isHidden() == (kind != "color")
        assert node.parm("use_clamp_result").isHidden() == (kind != "color")
        assert node.outputDataTypes() == (("float",) if kind == "float" else ("vector",)), node.outputDataTypes()
        assert node.inputDataTypes() == (
            ("float", "float", "float")
            if kind == "float"
            else ("vector", "vector", "vector" if non_uniform else "float")
        ), node.inputDataTypes()
        assert node.parm("use_clamp").parmTemplate().label() == "Clamp Factor"
        if check_color:
            rgb = {"color": (110, 110, 35), "vector": (60, 60, 131), "float": (36, 98, 131)}[kind]
            assert np.allclose(node.color().rgb(), np.asarray(rgb) / 255, atol=1e-6), node.color().rgb()
    drawn = _native.vop_connector_layout(node.sessionId())
    assert not any(p["group"] for p in drawn)
    assert [p["name"] for p in drawn] == list(data) + [factor], drawn
    names = [p.name() for p in node.parmTemplateGroup().entries()]
    assert names.index(factor if shader else pane_factor) > max(
        names.index(n) for n in (data if shader else pane_data)
    ), names
    node.updateParmStates()
    assert all(p.isDisabled() for p in node.parmTuple(factor if shader else pane_factor))
    assert node.inputLabels()[node.inputIndex(factor)] == "Factor"
    if shader:
        assert node.inputNames() == ("closure1", "closure2", "fac")
        assert node.inputLabels()[:2] == ("Shader 1", "Shader 2") and node.outputLabels() == ("Shader",)
        assert node.type().description() == "Cycles Mix Shader"
        for name in data:
            parm = node.parmTuple(name)
            assert parm.parmTemplate().type() == hou.parmTemplateType.Label and parm.isHidden()
    else:
        for name in ("a", "b", "a_float", "b_float", "a_vector", "b_vector", "fac", "fac_vector"):
            assert node.parmTuple(name).isHidden() == (name not in (*pane_data, pane_factor)), name
    return drawn


class Fixture(ShaderFixture):
    def __init__(self, device="CPU"):
        super().__init__(device)
        self.a = self.builder.createNode("cycles::color::1", "color_a")
        self.a.parmTuple("value").set(tuple(A))
        self.b = self.builder.createNode("cycles::color::1", "color_b")
        self.b.parmTuple("value").set(tuple(B))
        self.factor = self.builder.createNode("cycles::value::1", "factor")
        self.factor.parm("value").set(0.25)
        self.vector_factor = self.builder.createNode("cycles::color::1", "vector_factor")
        self.vector_factor.parmTuple("value").set((0.25,) * 3)
        self.shader_a = self.builder.createNode("cycles::emission::1", "shader_a")
        # The default this test was written for (new nodes now take Blender's).
        self.shader_a.setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        self.shader_b = self.builder.createNode("cycles::emission::1", "shader_b")
        # The default this test was written for (new nodes now take Blender's).
        self.shader_b.setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        for node, source in ((self.shader_a, self.a), (self.shader_b, self.b)):
            node.setNamedInput("color", source, "color")
            node.parm("strength").set(1)
        self.mix = self.builder.createNode("cycles::mix_color::1", "mix")
        self.shader_mix = self.builder.createNode("cycles::mix_closure::1", "mix_shader")
        self.shader_mix.setNamedInput("closure1", self.shader_a, "emission")
        self.shader_mix.setNamedInput("closure2", self.shader_b, "emission")
        self.shader_mix.setNamedInput("fac", self.factor, "value")
        self.float_a = self.builder.createNode("cycles::value::1", "float_a")
        self.float_a.parm("value").set(0.2)
        self.float_b = self.builder.createNode("cycles::value::1", "float_b")
        self.float_b.parm("value").set(0.6)
        self.nodes = {kind: self.shader_mix if kind == "mix_closure" else self.mix for kind in KINDS}
        for kind in KINDS:
            self.select(kind)
            check_layout(self.nodes[kind])
        self.builder.layoutChildren()

    def select(self, kind, factor=0.25):
        node = self.nodes[kind]
        node.bypass(False)
        self.factor.parm("value").set(factor)
        self.vector_factor.parmTuple("value").set((factor,) * 3)
        if kind != "mix_closure":
            node.setParms(
                dict(
                    data_type="vector" if kind == "vector_non_uniform" else kind,
                    factor_mode="non_uniform" if kind == "vector_non_uniform" else "uniform",
                )
            )
            for name, source in (
                ("a", self.float_a if kind == "float" else self.a),
                ("b", self.float_b if kind == "float" else self.b),
            ):
                node.setNamedInput(name, source, "value" if kind == "float" else "color")
            node.setNamedInput(
                "fac",
                self.vector_factor if kind == "vector_non_uniform" else self.factor,
                "color" if kind == "vector_non_uniform" else "value",
            )
        self.output.setNamedInput("surface", node, "closure" if kind == "mix_closure" else "result")
        return np.repeat(0.2 * (1 - factor) + 0.6 * factor, 3) if kind == "float" else A * (1 - factor) + B * factor
