# SPDX-License-Identifier: Apache-2.0
"""Native float/RGB ramps against independent piecewise and spatial references."""

from ramp_table_reference import native_lookup
from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import materials as m, native_scene
from cycles_houdini.ramps import lower_ramp

destination = artifacts_root() / "stage3/materials/ramps"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
assert ch.capabilities()["material_ramp_bases"] == [
    "constant",
    "linear",
    "catmull_rom",
    "monotone_cubic",
    "bezier",
    "bspline",
    "hermite",
]
assert ch.capabilities()["material_ramp_max_keys"] == 32
builder = ch.create_material(name="native_ramp_surface", shader="emission")
emission = builder.node("surface_shader")
emission.parm("strength").set(2.0)
color = builder.createNode("cycles::color_ramp::1", "color_ramp")
color.parm("ramp_interpolation").set("NATIVE")
# Float Ramp was retired: Float Curve with Range 0..1, Extension Horizontal
# and Factor 1 maps Value exactly like Float Ramp mapped its Factor.
assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::float_ramp::1") is None
scalar = builder.createNode("cycles::float_curve::1", "float_ramp")
scalar.parmTuple("curve_domain").set((0.0, 1.0))
scalar.parm("curve_extension").set(1)
scalar.parm("fac").set(1.0)


def as_float_ramp(node):
    node.parmTuple("curve_domain").set((0.0, 1.0))
    node.parm("curve_extension").set(1)
    node.parm("fac").set(1.0)


for node, is_color, name in ((color, True, "ramp"), (scalar, False, "curve_value")):
    ramp = node.parm(name).evalAsRamp()
    assert ramp.keys() == (0.0, 1.0)
    assert ramp.values() == (((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)) if is_color else (0.0, 1.0))
    assert ramp.basis() == (hou.rampBasis.Linear,) * 2
    assert node.parm(name + "1interp").parmTemplate().menuItems()[:2] == ("constant", "linear")
    assert len(node.parm(name + "1interp").parmTemplate().menuItems()) == 7
    assert node.inputNames() == (("factor",) if is_color else ("value", "fac")) and node.inputDataTypes() == (
        ("float",) if is_color else ("float", "float")
    )
    assert node.outputDataTypes() == (("vector", "float") if is_color else ("float",))
    assert not node.errors()

color_ramp = hou.Ramp(
    (hou.rampBasis.Linear, hou.rampBasis.Constant, hou.rampBasis.Linear, hou.rampBasis.Constant),
    (0.125, 0.3, 0.75, 0.875),
    ((0.2, 0.4, 0.8), (0.8, 0.2, 0.1), (0.1, 0.9, 0.4), (0.6, 0.3, 0.2)),
)
float_ramp = hou.Ramp(
    (hou.rampBasis.Constant, hou.rampBasis.Linear, hou.rampBasis.Constant, hou.rampBasis.Linear),
    (0.1, 0.25, 0.6, 0.9),
    (2.0, 4.0, 1.0, 3.0),
)
color.parm("ramp").set(color_ramp)
scalar.parm("curve_value").set(float_ramp)
emission.setNamedInput("color", color, "color")
factor = builder.createNode("cycles::value::1", "factor")
factor.parm("value").set(0.2125)
color.setNamedInput("factor", factor, "value")

# A second native network exercises a ramp factor crossing a typed subnet
# boundary. Both virtual hosts lower into owned engine nodes.
subnet = ch.create_subnet(builder, "nested_ramp")
port = ch.create_connector(subnet, "factor", default=0.5)
nested = subnet.createNode("cycles::float_curve::1", "float_ramp")
as_float_ramp(nested)
nested.setInput(nested.inputIndex("value"), port)
result = ch.create_connector(subnet, "value", kind="output")
result.setInput(0, nested)

obj = hou.node("/obj").createNode("geo", "ramp_receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
grid.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "ramp_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr", "native_ramp_cycles")
set_rop_devices(rop, "CPU")
for name, value in dict(
    soppath=grid.path(),
    camera=camera.path(),
    materialpath=builder.path(),
    resx=64,
    resy=64,
    samples=128,
    use_denoise=False,
).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))


def expect(frame, expected):
    data = array("f")
    data.frombytes(frame["planes"]["combined"]["pixels"])
    offset = (32 * 64 + 32) * 4
    measured = list(data[offset : offset + 3])
    assert np.allclose(measured, expected, atol=2e-5, rtol=2e-5), (measured, expected)
    return measured


def rejected(action, message):
    try:
        action()
    except (ValueError, RuntimeError) as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("Invalid ramp was accepted: " + message)


report = dict(
    result="running",
    host_checks=["Native float/RGB ports, two-key linear defaults and supported basis menus"],
    devices=[],
)
before = ch.material_graph(builder)
assert before["surface"]["node"] == emission.userData("cycles_graph_id")
assert not any(node["definition"] in ("cycles/float_ramp/1", "cycles/color_ramp/1") for node in before["nodes"])
color.setName("renamed_color_ramp")
assert ch.material_graph(builder) == before
report["host_checks"].append("Owned graph lowering is deterministic and preserves renamed identities")

# Each sample is specified independently of hou.Ramp.lookup and of the
# plugin compiler. Include held ends, nonuniform knots and both sides of a
# constant discontinuity. Small numeric tolerances reflect float32 math.
color_cases = [
    (-1.0, (0.2, 0.4, 0.8)),
    (0.125, (0.2, 0.4, 0.8)),
    (0.2125, (0.5, 0.3, 0.45)),
    (0.3, (0.8, 0.2, 0.1)),
    (0.5, (0.8, 0.2, 0.1)),
    (0.74999, (0.8, 0.2, 0.1)),
    (0.75, (0.1, 0.9, 0.4)),
    (0.8125, (0.35, 0.6, 0.3)),
    (0.875, (0.6, 0.3, 0.2)),
    (2.0, (0.6, 0.3, 0.2)),
]
float_cases = [
    (-1.0, 2.0),
    (0.1, 2.0),
    (0.24999, 2.0),
    (0.25, 4.0),
    (0.425, 2.5),
    (0.6, 1.0),
    (0.8, 1.0),
    (0.9, 3.0),
    (2.0, 3.0),
]

for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=64, height=64, samples=128, collect_diagnostics=True) as session:
        checks = []

        def render(name, expected, at_time=None):
            frame = session.wait(session.apply(native_scene.capture(rop, at_time=at_time), at_time=at_time))
            checks.append(dict(name=name, measured=expect(frame, expected)))
            return frame

        for position, value in color_cases:
            factor.parm("value").set(position)
            render(
                "Independent RGB piecewise reference at " + str(position),
                tuple(2.0 * entry for entry in native_lookup(color_ramp, position)),
            )
        conversions = session.status()["geometry_conversions"]
        emission.setInput(0, None)
        emission.parmTuple("color").set((0.2, 0.4, 0.8))
        emission.setNamedInput("strength", scalar, "value")
        for position, value in float_cases:
            scalar.parm("value").set(position)
            render(
                "Independent scalar piecewise reference at " + str(position),
                tuple(
                    float(native_lookup(float_ramp, position, interpolate=True)) * entry for entry in (0.2, 0.4, 0.8)
                ),
            )

        emission.setNamedInput("strength", subnet, "value")
        render("Ramp factor traverses a native typed subnet default", (0.1, 0.2, 0.4))
        nested.parm("value").set(9.0)
        render("Connected factor overrides the ignored native parameter", (0.1, 0.2, 0.4))

        # Animate a knot value before promoting the complete ramp. Public
        # whole-ramp references must preserve the knot's animation as well as
        # following added/deleted keys on the promoted control.
        scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (2.0, 4.0)))
        scalar.parm("curve_value2value").setExpression("$F + 3", hou.exprLanguage.Hscript)
        promoted = ch.promote_parameter(builder, scalar, "curve_value", name="gain_ramp", label="Gain Ramp")
        assert scalar.parm("curve_value").getReferencedParm() == promoted[0]
        scalar.parm("value").set(0.5)
        emission.setNamedInput("strength", scalar, "value")
        render("Animated whole-ramp promotion at explicit frame three", (0.8, 1.6, 3.2), hou.frameToTime(3))
        assert hou.frame() == 1
        for component in promoted[0].multiParmInstances():
            component.deleteAllKeyframes()
        promoted[0].set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0.0, 0.5, 1.0), (1.0, 3.0, 2.0)))
        render("Promoted ramp key-count change updates the source curve", (0.6, 1.2, 2.4))
        assert scalar.parm("curve_value").evalAsRamp().values() == (1.0, 3.0, 2.0)

        # Verify per-ray varying factors, rather than only compiler-folded
        # constants, against the independently specified orthographic camera.
        emission.setInput(1, None)
        emission.parm("strength").set(2.0)
        color.parm("ramp").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((0.0, 0.0, 0.0), (1.0, 0.5, 0.25))))
        geometry = builder.createNode("cycles::geometry::1", "coordinates")
        separate = builder.createNode("cycles::separate_xyz::1", "world_x")
        separate.setNamedInput("vector", geometry, "position")
        normalize = builder.createNode("cycles::math::1", "normalize_x")
        normalize.parm("math_type").set("multiply_add")
        normalize.parm("value2").set(0.25)
        normalize.parm("value3").set(0.5)
        normalize.setNamedInput("value1", separate, "x")
        color.setNamedInput("factor", normalize, "value")
        emission.setNamedInput("color", color, "color")
        frame = session.wait(session.apply(native_scene.capture(rop)))
        image = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(64, 64, 4)
        target = 2.0 * ((np.arange(64) + 0.5) / 64.0)[None, :, None] * np.array((1.0, 0.5, 0.25))[None, None, :]
        assert np.max(np.abs(image[:, :, :3] - target)) < 0.003, np.max(np.abs(image[:, :, :3] - target))
        assert np.mean(np.abs(image[:, :, :3] - target)) < 0.0005
        checks.append(
            dict(
                name="Per-ray ramp variation matches independent orthographic pixel references",
                max_error=float(np.max(np.abs(image[:, :, :3] - target))),
            )
        )
        # The scalar ramp also needs a varying factor to exercise GPU code.
        scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (1.0, 3.0)))
        scalar.setNamedInput("value", normalize, "value")
        emission.setInput(0, None)
        emission.parmTuple("color").set((0.2, 0.4, 0.8))
        emission.setNamedInput("strength", scalar, "value")
        frame = session.wait(session.apply(native_scene.capture(rop)))
        image = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(64, 64, 4)
        target = (1.0 + 2.0 * ((np.arange(64) + 0.5) / 64.0))[None, :, None] * np.array((0.2, 0.4, 0.8))[None, None, :]
        assert np.max(np.abs(image[:, :, :3] - target)) < 0.003
        assert np.mean(np.abs(image[:, :, :3] - target)) < 0.0005
        checks.append(dict(name="Per-ray scalar ramp matches independent orthographic pixel references"))
        color.setInput(0, factor)
        scalar.setInput(scalar.inputIndex("value"), None)
        normalize.destroy()
        separate.destroy()
        geometry.destroy()

        # Broken authored controls keep geometry visible and recover locally.
        scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.5, 0.5), (1.0, 2.0)))
        render("Duplicate knots use diagnostic shading", (1.0, 0.0, 1.0))
        assert session.status()["source_diagnostics"]
        scalar.parm("curve_value").set(float_ramp)
        scalar.parm("value").set(0.425)
        render("Malformed ramp preserves the live generation and recovers", (0.5, 1.0, 2.0))
        allocations = []
        for iteration in range(20):
            scalar.parm("curve_value").set(
                hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (2.0, 4.0 if iteration % 2 else 2.0))
            )
            scalar.parm("value").set(0.5)
            frame = session.wait(session.apply(native_scene.capture(rop)))
            expect(frame, (0.6, 1.2, 2.4) if iteration % 2 else (0.4, 0.8, 1.6))
            allocations.append(session.status()["device_allocation_bytes"])
        assert len(set(allocations[5:])) == 1 and session.status()["geometry_conversions"] == conversions
        checks.append(
            dict(name="Repeated ramp edits retain geometry and stable tracked allocations", allocations=allocations)
        )

        # Reset reference ownership and scene state for the next device.
        scalar.parm("curve_value").deleteAllKeyframes()
        scalar.parm("curve_value").set(float_ramp)
        group = builder.parmTemplateGroup()
        group.remove("gain_ramp")
        builder.setParmTemplateGroup(group)
        color.parm("ramp").set(color_ramp)
        factor.parm("value").set(0.2125)
        emission.setInput(1, None)
        emission.parm("strength").set(2.0)
        emission.setNamedInput("color", color, "color")
        report["devices"].append(dict(device=device, checks=checks))
    assert session.status()["device_allocation_bytes"] == 0

# Exercise bounds and type guards directly with owned values; host UI may
# normalize malformed keys before the adapter gets a chance to see them.
scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.CatmullRom,) * 2, (0.0, 1.0), (1.0, 2.0)))
emission.setNamedInput("strength", scalar, "value")
assert any(n["definition"] == "cycles/float_curve/1" for n in ch.material_graph(builder)["nodes"])
scalar.parm("curve_value").set(float_ramp)
scalar.parm("curve_value").set(
    hou.Ramp((hou.rampBasis.Linear,) * 33, tuple(index / 32 for index in range(33)), (1.0,) * 33)
)
fault = next(
    n for n in ch.material_graph(builder)["nodes"] if n["id"] == scalar.userData("cycles_graph_id") + "/native"
)
assert fault["parameters"]["source"] == [0x43525331, 3, 0, 1, 0, 0, 0, 0], fault
scalar.parm("curve_value").set(float_ramp)
expanded = []
# Each Float Curve lowers to one stock curve plus its Factor default. Chain
# enough to cross the 256-node flattened-graph budget.
for index in range(260):
    node = builder.createNode("cycles::float_curve::1", "expanded" + str(index))
    as_float_ramp(node)
    node.parm("curve_value").set(
        hou.Ramp(
            (hou.rampBasis.Linear,) * 32, tuple(key / 31 for key in range(32)), tuple(float(key) for key in range(32))
        )
    )
    expanded.append(node)
# Disconnected experimental ramps are intentionally ignored. Exercise the
# graph-size guard only after connecting all three expansions to the output.
for index, node in enumerate(expanded):
    if index:
        node.setNamedInput("value", expanded[index - 1], "value")
emission.setNamedInput("strength", expanded[-1], "value")
rejected(lambda: ch.material_graph(builder), "256 shader nodes")
for node in expanded:
    node.destroy()
rejected(lambda: lower_ramp("invalid", ("linear",), (0.0,), (float("nan"),), False), "finite float32")
rejected(lambda: lower_ramp("invalid", ("linear",), (2.0,), (1.0,), False), "[0,1]")
rejected(lambda: lower_ramp("invalid", ("unknown",), (0.0,), (1.0,), False), "Unknown native ramp")
rejected(
    lambda: lower_ramp("invalid", ("linear",) * 33, tuple(index / 32 for index in range(33)), (1.0,) * 33, False),
    "1..32",
)
rejected(lambda: lower_ramp("invalid", ("linear",), (0.0,), ((1.0, 2.0),), True), "three components")
report["host_checks"].append("Native spline lowering, knot count and reachable graph-budget guards")
report["host_checks"].append("Explicit finite, domain, count, interpolation and vector-arity guards")

before = ch.material_graph(builder)
hip = destination / "native-material-ramps.hiplc"
hou.hipFile.save(str(hip))
hou.hipFile.load(str(hip), suppress_save_prompt=True)
builder = hou.node("/mat/native_ramp_surface")
rop = hou.node("/out/native_ramp_cycles")
assert ch.material_graph(builder) == before
report["host_checks"].append("Saved native ramps preserve keys, bases, definitions and exact lowered graph")
rop.parm("picture").set(str(destination / "ramp-frame.$F4.exr"))
factor = builder.node("factor")
factor.parm("value").setExpression(".125 + ($F-1)*.0875", hou.exprLanguage.Hscript)
rop.render(frame_range=(1, 3, 1))
for frame in (1, 2, 3):
    expected = 2 * native_lookup(color_ramp, 0.125 + (frame - 1) * 0.0875)
    image = OIIO.ImageInput.open(str(destination / f"ramp-frame.{frame:04}.exr"))
    assert image
    pixels = image.read_image(format=OIIO.FLOAT)
    image.close()
    assert np.allclose(pixels[32, 32, :3], expected, atol=2e-5, rtol=2e-5)
report["host_checks"].append("Three native SOHO batch frames match independent nonuniform-ramp references")
report["result"] = "passed"
report["scope"] = (
    "Native RGB and scalar Constant/Linear ramps with 1..32 strictly increasing float32 keys; selected CPU/4090 radiance, varying factors, promotion, animation and lifecycle. Other native interpolation/color models are covered by houdini_ramp_interpolation_smoke.py."
)
(destination / "houdini-material-ramp-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            host_checks=len(report["host_checks"]),
            checks_per_device=[len(row["checks"]) for row in report["devices"]],
        )
    )
)
