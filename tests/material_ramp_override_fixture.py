# SPDX-License-Identifier: Apache-2.0
"""Native Material SOP ramp/curve dictionaries with independent image references."""

import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection
from light_shader_fixture import Fixture as LightFixture
import native_curve_reference as curve_reference
from native_curve_reference import CASES as CURVE_CASES

ROOT = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
RGB_CASES = {
    row["id"]: row for row in json.loads((ROOT / "tests/fixtures/blender-rgb-ramps.json").read_text())["cases"]
}
RGB_TABLES = json.loads((ROOT / "tests/fixtures/blender-rgb-ramps-tables.json").read_text())
SIMPLE_STOPS = ((0.0, (0.1, 0.3, 0.5), 0.2), (0.5, (0.9, 0.5, 0.1), 0.8), (1.0, (0.2, 0.6, 0.9), 0.4))


class Fixture(LightFixture):
    def __init__(self, device, *, heavy=False):
        targets = json.loads(device) if isinstance(device, str) and device.lstrip().startswith("[") else [device]
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(value, str) or not value for value in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("Expected one device or a nonempty JSON array of unique exact device IDs")
        super().__init__(targets[0])
        device_selection.set_devices(self.rop, targets)
        self.device = device
        self.target_ids = sorted(targets)
        pass_fixture.set_parameters(
            self.rop, dict(use_material_assignments=1, samples=1, ipr_samples=1, pass_position=1, pass_normal=1)
        )
        if heavy:
            self.add_heavy_packed()
            pass_fixture.set_parameters(self.sop, dict(rows=257, cols=513))
        self.material = ch.create_material(name="native_ramp_override_material", shader="emission")
        self.emission = self.material.node("surface_shader")
        pass_fixture.parm(self.emission, "strength").set(1)
        self.rgba = self.material.createNode("cycles::color_ramp::1", "color_ramp")
        pass_fixture.parm(self.rgba, "ramp").set(
            hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((0.2, 0.4, 0.6), (0.2, 0.4, 0.6)))
        )
        pass_fixture.parm(self.rgba, "ramp_interpolation").set("EASE")
        pass_fixture.parm(self.rgba, "factor").set(0.75)
        ch.promote_parameter(self.material, self.rgba, "ramp", name="palette", label="Color Ramp")
        # Float Curve replaces Float Ramp: Range 0..1, Horizontal, Factor 1.
        self.profile = self.material.createNode("cycles::float_curve::1", "float_ramp")
        pass_fixture.parm(self.profile, "value").set(0.75)
        self.profile.parmTuple("curve_domain").set((0.0, 1.0))
        pass_fixture.parm(self.profile, "curve_extension").set(1)
        pass_fixture.parm(self.profile, "fac").set(1)
        pass_fixture.parm(self.profile, "curve_value").set(
            hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.1, 0.9))
        )
        ch.promote_parameter(self.material, self.profile, "curve_value", name="profile", label="Float Curve")
        self.curves = {}
        for kind in ("float_curve", "rgb_curves", "vector_curves"):
            node = self.material.createNode("cycles::" + kind + "::1", kind)
            pass_fixture.parm(node, "fac").set(1)
            if kind == "float_curve":
                pass_fixture.parm(node, "value").set(0.425)
            else:
                node.parmTuple("value").set((0.425,) * 3)
            for channel in curve_reference.CHANNELS[kind]:
                ch.promote_parameter(
                    self.material, node, "curve_" + channel, name=kind + "_" + channel, label=channel.upper() + " Curve"
                )
            self.curves[kind] = node
        self.offset = self.material.createNode("cycles::vector_displacement::1", "independent_displacement")
        # The default this test was written for (new nodes now take Blender's).
        self.offset.setParms(dict(scale=1.0, space="object"))
        self.offset.parmTuple("vector").set((0, 0, 0.15))
        ch.promote_parameter(self.material, self.offset, "vector", name="offset")
        self.material.node("surface_output").setNamedInput("displacement", self.offset, "displacement")
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        self.assigned = self.obj.createNode("material", "native_ramp_overrides")
        self.assigned.setInput(0, self.sop)
        pass_fixture.parm(self.assigned, "shop_materialpath1").set(self.material.path())
        self.assigned.setDisplayFlag(True)
        self.assigned.setRenderFlag(True)
        self.literal = self.obj.createNode("attribwrangle", "partial_native_override_dictionary")
        self.literal.setInput(0, self.assigned)
        pass_fixture.parm(self.literal, "class").set(1)
        self.literal.addSpareParmTuple(hou.StringParmTemplate("payload", "Override Literal", 1))
        pass_fixture.parm(self.literal, "snippet").set('s@material_override=chs("payload");')
        self.indices = {}
        self.simple_rgba()

    def set_fields(self, fields):
        """Author only stock Material SOP Local Overrides and value controls."""
        self.fields = list(fields)
        self.indices = {name: i for i, (name, _, _) in enumerate(fields, 1)}
        pass_fixture.parm(self.assigned, "num_local1").set(len(fields))
        for i, (name, kind, value) in enumerate(fields, 1):
            pass_fixture.parm(self.assigned, f"local1_name{i}").set(name)
            pass_fixture.parm(self.assigned, f"local1_type{i}").set(kind)
            suffix = {
                "int": "ival",
                "float": "fval",
                "string": "sval",
                "color": "cval",
                "vector2": "vval",
                "vector3": "vval",
                "vector4": "vval",
            }[kind]
            target = self.assigned.parmTuple(f"local1_{suffix}{i}")
            values = tuple(value) if isinstance(value, (tuple, list)) else (value,)
            if kind.startswith("vector"):
                values = values + (0.0,) * (len(target) - len(values))
            target.set(values)
        self.assigned.setDisplayFlag(True)
        self.assigned.setRenderFlag(True)

    def local(self, name, control="name"):
        return pass_fixture.parm(self.assigned, f"local1_{control}{self.indices[name]}")

    def literal_fields(self):
        attribute = self.assigned.geometry().findPrimAttrib("material_override")
        assert attribute
        return self.assigned.geometry().prim(0).stringAttribValue(attribute)

    def use_literal(self, literal):
        pass_fixture.parm(self.literal, "payload").set(literal)
        self.literal.setDisplayFlag(True)
        self.literal.setRenderFlag(True)

    def surface(self, node, output="color", alpha=False):
        self.emission.setNamedInput("color", node, output)
        self.emission.setInput(self.emission.inputIndex("strength"), None)
        pass_fixture.parm(self.emission, "strength").deleteAllKeyframes()
        pass_fixture.parm(self.emission, "strength").set(1, follow_parm_reference=False)
        if alpha:
            self.emission.setNamedInput("strength", node, "alpha")

    def independent_height(self):
        self.material.node("surface_output").setNamedInput("displacement", self.offset, "displacement")
        for name in ("_alpha_height_sum", "_alpha_height", "_curve_height_sum", "_curve_height"):
            node = getattr(self, name, None)
            if node is not None:
                node.destroy()
                setattr(self, name, None)

    def alpha_height_case(self):
        """A separate fixed height plus the valid overridden Alpha contribution."""
        self.independent_height()
        pass_fixture.parm(self.rgba, "factor").set(0.75)
        self.surface(self.rgba, alpha=True)
        self.set_fields([("palette_alpha1value", "float", 0.8), ("palette_interpolation", "string", "LINEAR")])
        self._authored_rgb = pass_fixture.parm(self.material, "palette1cr").eval()
        self._alpha_height = self.material.createNode("cycles::vector_displacement::1", "independent_alpha_height")
        # The default this test was written for (new nodes now take Blender's).
        self._alpha_height.setParms(dict(space="object"))
        self._alpha_height.parmTuple("vector").set((0, 0, 0.1))
        self._alpha_height.setNamedInput("scale", self.rgba, "alpha")
        self._alpha_height_sum = self.material.createNode("cycles::vector_math::1", "independent_height_sum")
        pass_fixture.parm(self._alpha_height_sum, "math_type").set("add")
        self._alpha_height_sum.setNamedInput("vector1", self.offset, "displacement")
        self._alpha_height_sum.setNamedInput("vector2", self._alpha_height, "displacement")
        self.material.node("surface_output").setNamedInput("displacement", self._alpha_height_sum, "vector")
        return np.asarray((0.19, 0.38, 0.57)), 0.245

    def authored_rgb_fault(self, broken):
        pass_fixture.parm(self.material, "palette1cr").set(float("nan") if broken else self._authored_rgb)
        return np.asarray((1.0, 0.0, 1.0) if broken else (0.19, 0.38, 0.57))

    def curve_height_case(self):
        """A valid overridden endpoint independently drives a separate height."""
        self.independent_height()
        node = self.curves["float_curve"]
        self.surface(node, "value")
        prefix = "float_curve_value"
        pass_fixture.parm(self.material, prefix).set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.0, 1.0)))
        pass_fixture.parm(node, "fac").set(1)
        pass_fixture.parm(node, "value").set(0.425)
        node.parmTuple("curve_domain").set((0.0, 1.0))
        pass_fixture.parm(node, "curve_extension").set(0)
        endpoint = pass_fixture.parm(self.material, prefix + "2value")
        self._curve_start_x = pass_fixture.parm(self.material, prefix + "1pos")
        self._authored_curve_x = self._curve_start_x.eval()
        self.set_fields([(endpoint.name(), "float", 0.8)])
        self._curve_height = self.material.createNode(
            "cycles::vector_displacement::1", "independent_curve_endpoint_height"
        )
        # The default this test was written for (new nodes now take Blender's).
        self._curve_height.setParms(dict(space="object"))
        self._curve_height.parmTuple("vector").set((0.0, 0.0, 0.1))
        pass_fixture.parm(self._curve_height, "scale").setExpression(
            'ch("../' + endpoint.name() + '")', hou.exprLanguage.Hscript
        )
        self._curve_height_sum = self.material.createNode("cycles::vector_math::1", "independent_curve_height_sum")
        pass_fixture.parm(self._curve_height_sum, "math_type").set("add")
        self._curve_height_sum.setNamedInput("vector1", self.offset, "displacement")
        self._curve_height_sum.setNamedInput("vector2", self._curve_height, "displacement")
        self.material.node("surface_output").setNamedInput("displacement", self._curve_height_sum, "vector")
        return np.asarray((0.34,) * 3), 0.23

    def authored_curve_fault(self, broken):
        self._curve_start_x.set(float("nan") if broken else self._authored_curve_x)
        return np.asarray((1.0, 0.0, 1.0) if broken else (0.34,) * 3)

    def simple_rgba(self, alpha=0.4):
        self.independent_height()
        pass_fixture.parm(self.rgba, "factor").set(0.75)
        self.surface(self.rgba, alpha=True)
        # Color stops on the promoted Color ramp, Alpha stops (same positions)
        # on the promoted Alpha ramp; EASE applies to both.
        fields = [("palette", "int", 3), ("palette_interpolation", "string", "EASE"), ("palette_alpha", "int", 3)]
        for i, (position, color, a) in enumerate(SIMPLE_STOPS, 1):
            fields.extend(
                [
                    (f"palette{i}pos", "float", position),
                    (f"palette{i}c", "color" if i == 1 else "vector3", color),
                    (f"palette{i}interp", "int", 1),
                    (f"palette_alpha{i}pos", "float", position),
                    (f"palette_alpha{i}value", "float", alpha if i == 3 else a),
                    (f"palette_alpha{i}interp", "int", 1),
                ]
            )
        self.set_fields(fields)
        return np.asarray((0.55, 0.55, 0.5)) * ((0.8 + alpha) * 0.5)

    def rgb_reference(self, row):
        self.independent_height()
        pass_fixture.parm(self.rgba, "factor").set(0.25)
        self.surface(self.rgba, alpha=True)
        fields = [
            ("palette", "int", len(row["controls"])),
            ("palette_alpha", "int", len(row["controls"])),
            ("palette_interpolation", "string", row["interpolation"]),
            ("palette_color_space", "string", row["mode"]),
            ("palette_hue_interpolation", "string", row["hue"]),
        ]
        for i, stop in enumerate(row["controls"], 1):
            fields.extend(
                [
                    (f"palette{i}pos", "float", stop["position"]),
                    (f"palette{i}c", "color", stop["color"]),
                    (f"palette{i}interp", "int", 1),
                    (f"palette_alpha{i}pos", "float", stop["position"]),
                    (f"palette_alpha{i}value", "float", stop["alpha"]),
                    (f"palette_alpha{i}interp", "int", 1),
                ]
            )
        self.set_fields(fields)
        index = row["factors"].index(0.25)
        reference = RGB_TABLES[row["id"]]
        return np.asarray(reference["colors"][index]) * reference["alphas"][index]

    def scalar_ramp(self):
        self.independent_height()
        pass_fixture.parm(self.profile, "value").set(0.75)
        self.surface(self.profile, "value")
        self.set_fields(
            [
                ("profile", "int", 3),
                ("profile1pos", "float", 0.0),
                ("profile1value", "float", 0.2),
                ("profile1interp", "int", 1),
                ("profile2pos", "float", 0.5),
                ("profile2value", "float", 0.8),
                ("profile2interp", "int", 1),
                ("profile3pos", "float", 1.0),
                ("profile3value", "float", 0.4),
                ("profile3interp", "int", 1),
            ]
        )
        return np.asarray((0.6,) * 3)

    def curve_reference(self, row):
        """A whole native-stop dictionary for every channel of one curve node."""
        self.independent_height()
        kind = row["kind"]
        node = self.curves[kind]
        self.surface(node, "value")
        node.parmTuple("curve_domain").set(row["domain"])
        pass_fixture.parm(node, "curve_extension").set(0)
        fields = []
        for channel, (bases, keys, values) in zip(curve_reference.CHANNELS[kind], row["channels"]):
            prefix = kind + "_" + channel
            fields.append((prefix, "int", len(keys)))
            for i, (basis, key, value) in enumerate(zip(bases, keys, values), 1):
                fields.extend(
                    [
                        (f"{prefix}{i}pos", "float", key),
                        (f"{prefix}{i}value", "float", value),
                        (f"{prefix}{i}interp", "int", curve_reference.BASES.index(basis)),
                    ]
                )
        self.set_fields(fields)
        self.curve_row = row
        value = 0.425 if kind == "float_curve" else (0.425,) * 3
        result = curve_reference.evaluate(kind, row["channels"], row["domain"], True, value)
        return np.broadcast_to(np.asarray(result, float), (3,)).copy()

    def lazy_expression_case(self):
        """Relative expressions are captured before any authored value probing."""
        self.independent_height()
        for name in ("color", "strength"):
            self.emission.setInput(self.emission.inputIndex(name), None)
        self.emission.parmTuple("color").set((0.1, 0.2, 0.4))
        if pass_fixture.parm(self.material, "lazy_power") is None:
            self.material.addSpareParmTuple(hou.FloatParmTemplate("lazy_power", "Lazy Power", 1, default_value=(2.0,)))
        pass_fixture.parm(self.emission, "strength").deleteAllKeyframes()
        pass_fixture.parm(self.emission, "strength").setExpression('ch("../lazy_power")*2', hou.exprLanguage.Hscript)
        height = tuple(self.material.parmTuple("offset"))[2]
        self.set_fields([("lazy_power", "float", 7.0), (height.name(), "float", 0.23)])
        return np.asarray((1.4, 2.8, 5.6)), 0.23

    def lazy_expression_python(self, broken):
        expression = 'hou.parm("../lazy_power").eval()*2' if broken else 'ch("../lazy_power")*2'
        pass_fixture.parm(self.emission, "strength").setExpression(
            expression, hou.exprLanguage.Python if broken else hou.exprLanguage.Hscript
        )
        return np.asarray((0.4, 0.8, 1.6) if broken else (1.4, 2.8, 5.6))

    def authored(self):
        # Even keyframes() primes Houdini Python channel dependencies. rawValue()
        # is passive, so the first active capture still has to discover them.
        return tuple(
            (parm.path(), parm.rawValue())
            for node in (self.material, *self.material.allSubChildren())
            for parm in node.parms()
        )

    def assert_pixels(self, frame, expected, z=0.15):
        np.testing.assert_allclose(
            self.pixels(frame)[31:33, 31:33, :3], np.broadcast_to(expected, (2, 2, 3)), atol=7e-5, rtol=3e-5
        )
        np.testing.assert_allclose(self.pixels(frame, "position")[31:33, 31:33, 2], z, atol=2e-5, rtol=0)
