# SPDX-License-Identifier: Apache-2.0
"""Native VOP ports, animation, promotion, saved identities and ROP rendering."""

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
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/vops"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
builder = ch.create_material(name="native_surface", shader="emission")
emission = builder.node("surface_shader")
output = builder.node("surface_output")
assert builder.isMaterialFlagSet() and builder.childTypeCategory() == hou.vopNodeTypeCategory()
assert emission.shaderLanguageName() == "Cycles"
assert emission.inputNames() == ("color", "strength")
assert emission.inputDataTypes() == ("vector", "float")
assert emission.outputNames() == ("emission",) and emission.outputDataTypes() == ("bsdf",)
# Blender 5.3's Emission defaults (docs/user/rop-defaults-and-layout.md, Shader node defaults).
assert emission.parm("strength").eval() == 1
assert np.allclose(emission.parmTuple("color").eval(), (1.0, 1.0, 1.0), atol=1e-7)
assert emission.userData("cycles_graph_id")

# Independent UI expectations exercise registration independently of rendering.
catalogue = ch.shader_catalogue()
catalogue_builder = hou.node("/mat").createNode("cycles::material::1", "catalogue_ui", run_init_scripts=False)
for definition in catalogue["definitions"]:
    if not definition["surface_graph_adapter"]:
        continue
    if definition["engine_type"] in ("mix", "mix_float", "mix_vector", "mix_vector_non_uniform"):
        assert hou.nodeType(hou.vopNodeTypeCategory(), f"cycles::{definition['engine_type']}::1") is None
        continue
    node = catalogue_builder.createNode(f"cycles::{definition['engine_type']}::1")
    node.updateParmStates()
    assert node.shaderLanguageName() == "Cycles"
    expected_inputs = tuple(
        socket["id"]
        for socket in definition["inputs"]
        if socket["linkable"]
        and not socket["internal_svm"]
        and not (definition["engine_type"] == "bump" and socket["id"] in ("sample_center", "sample_x", "sample_y"))
    )
    if definition["engine_type"] in ("fresnel", "layer_weight"):
        # Normal (OSL-internal default, still a port) follows IOR / Blend, as in Blender.
        expected_inputs = tuple(name for name in expected_inputs if name != "normal") + ("normal",)
    if definition["engine_type"] in (
        "mix",
        "mix_closure",
        "mix_color",
        "mix_float",
        "mix_vector",
        "mix_vector_non_uniform",
        "float_curve",
        "rgb_curves",
        "vector_curves",
    ):
        expected_inputs = tuple(name for name in expected_inputs if name != "fac") + ("fac",)
    if definition["engine_type"].startswith("index_switch_"):
        # The catalogue's item0 is a prototype; the native multiparm starts
        # with two independently addressable items and expands its ports.
        assert node.parm("item_count").eval() == 2
        expected_inputs = ("index", "item0", "item1")
        node.parm("item_count").set(3)
        assert node.inputNames() == ("index", "item0", "item1", "item2")
        node.parm("item_count").set(2)
    assert node.inputNames() == expected_inputs, (definition["engine_type"], node.inputNames(), expected_inputs)
    assert node.outputNames() == tuple(socket["id"] for socket in definition["outputs"])
    if definition["engine_type"] == "attribute":
        assert node.parm("attribute").eval() == "Cd", "Authored Houdini Cd default is missing"
    for socket in definition["inputs"]:
        if socket["default_links"]:
            parm = node.parmTuple(socket["id"])
            assert parm is None or (parm.parmTemplate().type() == hou.parmTemplateType.Label and parm.isHidden()), (
                "Implicit engine defaults became editable host constants",
                definition["engine_type"],
                socket["id"],
            )
    assert not node.errors()
catalogue_builder.destroy()
try:
    builder.createNode("constant")
except hou.OperationFailed:
    pass
else:
    raise AssertionError("The Cycles context accepted a VEX-only child")

color = builder.createNode("cycles::color::1", "base_color")
color.parmTuple("value").set((0.2, 0.4, 0.8))
value = builder.createNode("cycles::value::1", "unit")
value.parm("value").set(1.0)
math = builder.createNode("cycles::math::1", "gain")
math.parm("math_type").set("multiply")
assert math.parm("math_type").evalAsString() == "multiply"
assert "multiply" in math.parm("math_type").parmTemplate().menuItems()
math.parm("value2").setExpression("$F + 1", hou.exprLanguage.Hscript)
promoted = ch.promote_parameter(builder, math, "value2", name="gain", label="Emission Gain")
assert promoted[0].expression() == "$F + 1"
assert math.parm("value2").getReferencedParm() == promoted[0]
dot1 = builder.createNetworkDot()
dot1.setInput(color, 0)
dot2 = builder.createNetworkDot()
dot2.setInput(dot1)
emission.setInput(0, dot2)
math.setNamedInput("value1", value, "value")
emission.setNamedInput("strength", math, "value")
try:
    ch.promote_parameter(builder, emission, "strength")
except ValueError as error:
    assert "Disconnect" in str(error)
else:
    raise AssertionError("A connected, ignored constant was promoted")

obj = hou.node("/obj").createNode("geo", "native_vop_receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
grid.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "native_vop_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr", "native_material_cycles")
set_rop_devices(rop, "CPU")
for name, value in dict(
    soppath=grid.path(),
    camera=camera.path(),
    materialpath=rop.relativePathTo(builder),
    resx=64,
    resy=64,
    samples=32,
    use_denoise=0,
    ipr_use_denoise=0,
).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))


def pixel(frame):
    data = array("f")
    data.frombytes(frame["planes"]["combined"]["pixels"])
    offset = (32 * 64 + 32) * 4
    return list(data[offset : offset + 3])


def expect(frame, expected):
    measured = pixel(frame)
    assert np.allclose(measured, expected, atol=1e-5, rtol=1e-5), (measured, expected)
    return measured


graph = ch.material_graph(builder, at_time=hou.frameToTime(3))
gain = next(node for node in graph["nodes"] if node["definition"] == "cycles/math/1")
assert gain["parameters"]["value2"] == 4
assert len(graph["links"]) == 3 and hou.frame() == 1
assert ch.material_graph(builder) == ch.material_graph(builder)
old_graph = ch.material_graph(builder)
emission.setName("renamed_emission")
assert ch.material_graph(builder) == old_graph, "Rename changed a saved graph identity"

notifications = []
observer = native_scene.SceneObserver(rop, lambda: notifications.append(True))
color.parmTuple("value").set((0.25, 0.4, 0.8))
assert notifications
notifications.clear()
extra = builder.createNode("cycles::value::1", "temporary")
assert notifications
observer.refresh()
notifications.clear()
extra.parm("value").set(2)
assert notifications
extra.destroy()
observer.refresh()
observer.close()
color.parmTuple("value").set((0.2, 0.4, 0.8))

report = dict(
    result="running",
    native_definitions=len([item for item in catalogue["definitions"] if item["surface_graph_adapter"]]),
    host_checks=[
        "Public custom language and typed closure ports",
        "Pinned actual defaults and implicit normal/tangent defaults",
        "Complete accepted catalogue registered in the custom context",
        "VEX-only nodes excluded",
        "Named enum storage",
        "Parameter promotion preserves animation and channel references",
        "Network dots flatten without changing output indices",
        "Explicit-time graph evaluation preserves the current frame",
        "Read-only deterministic extraction and stable rename identity",
        "Relative ROP binding and graph creation/deletion/parameter notifications",
    ],
    devices=[],
)
devices = ["CPU", ch.default_device()]
for device in devices:
    with ch.Session(device=device, width=64, height=64, samples=32, collect_diagnostics=True) as session:
        checks = []
        frame = session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
        checks.append(dict(name="Independent authored HDR radiance", measured=expect(frame, (0.4, 0.8, 1.6))))
        conversions = session.status()["geometry_conversions"]
        material_builds = session.diagnostics()["material_builds"]
        node_graph = ch.material_graph(builder)
        node_id = emission.userData("cycles_graph_id")
        emission.setName("another_name")
        assert ch.material_graph(builder) == node_graph and emission.userData("cycles_graph_id") == node_id
        frame = session.wait(session.apply(native_scene.capture(rop)))
        assert session.diagnostics()["material_builds"] == material_builds
        assert session.status()["geometry_conversions"] == conversions
        checks.append(dict(name="Rename retains material and geometry"))
        frame = session.wait(
            session.apply(native_scene.capture(rop, at_time=hou.frameToTime(3)), at_time=hou.frameToTime(3))
        )
        checks.append(
            dict(name="Animated promoted parameter at explicit render time", measured=expect(frame, (0.8, 1.6, 3.2)))
        )
        assert hou.frame() == 1 and session.status()["geometry_conversions"] == conversions
        initial_generation = session.status()["generation"]
        duplicate_output = builder.createNode("cycles::surface_output::1")
        # The raw graph API remains strict, but a broken assigned host graph is
        # a recoverable source problem. It must not stop an artist's render.
        try:
            ch.material_graph(builder)
        except ValueError as error:
            assert "exactly one" in str(error)
        else:
            raise AssertionError("Raw graph extraction accepted ambiguous outputs")
        frame = session.wait(session.apply(native_scene.capture(rop)))
        expect(frame, (1, 0, 1))
        status = session.status()
        assert not status["error"] and status["generation"] > initial_generation
        assert status["geometry_conversions"] == conversions
        issues = status["source_diagnostics"]
        assert len(issues) == 1 and issues[0]["code"] == "material.invalid_override", issues
        assert rop.node(issues[0]["source"]) == builder and "exactly one" in issues[0]["message"], issues
        duplicate_output.destroy()
        frame = session.wait(session.apply(native_scene.capture(rop)))
        expect(frame, (0.4, 0.8, 1.6))
        assert not session.status()["source_diagnostics"]
        assert session.status()["geometry_conversions"] == conversions
        checks.append(
            dict(
                name="Ambiguous ROP override renders diagnostic shading; repair clears warnings without geometry conversion; raw graph extraction remains strict"
            )
        )
        reports = []
        for iteration in range(20):
            color.parmTuple("value").set((0.3 if iteration % 2 else 0.2, 0.4, 0.8))
            frame = session.wait(session.apply(native_scene.capture(rop)))
            expect(frame, (0.6 if iteration % 2 else 0.4, 0.8, 1.6))
            reports.append(session.status()["device_allocation_bytes"])
        assert len(set(reports[5:])) == 1 and session.status()["geometry_conversions"] == conversions
        color.parmTuple("value").set((0.2, 0.4, 0.8))
        checks.append(
            dict(name="Repeated native parameter edits retain geometry and stable tracked memory", allocations=reports)
        )
        report["devices"].append(dict(device=device, checks=checks))
    assert session.status()["device_allocation_bytes"] == 0

# Save/load is a separate host lifecycle and must preserve the exact definition
# versions, socket tokens, identities, promoted expressions and network dots.
texture = builder.createNode("cycles::noise_texture::1", "saved_texture")
texture.parm("scale").setExpression("$F + 1", hou.exprLanguage.Hscript)
mapping = builder.createNode("cycles::mapping::1", "texture_mapping")
mapping.parmTuple("location").set((0.25, -0.5, 2))
mapping.parmTuple("scale").set((2, 1, 1))
texture.setNamedInput("vector", mapping, "vector")
assert all(not p.name().startswith("tex_mapping.") for p in texture.parms())
texture_id = texture.userData("cycles_graph_id")
before = ch.material_graph(builder)
assert texture_id not in {node["id"] for node in before["nodes"]}, "Disconnected node entered the render graph"
emission.setNamedInput("color", texture, "color")
mapped = ch.material_graph(builder)
parameters = next(node["parameters"] for node in mapped["nodes"] if node["id"] == texture_id)
assert not any(name.startswith("tex_mapping.") for name in parameters)
assert parameters["scale"] == 2
emission.setInput(0, dot2)
assert ch.material_graph(builder) == before
hip = destination / "native-material-vops.hiplc"
hou.hipFile.save(str(hip))
hou.hipFile.load(str(hip), suppress_save_prompt=True)
builder = hou.node("/mat/native_surface")
rop = hou.node("/out/native_material_cycles")
assert ch.material_graph(builder) == before
assert builder.parm("gain").expression() == "$F + 1"
assert len(builder.networkDots()) == 2
texture = builder.node("saved_texture")
mapping = builder.node("texture_mapping")
assert texture.userData("cycles_graph_id") == texture_id
assert texture.parm("scale").expression() == "$F + 1"
assert mapping.parmTuple("location").eval() == (0.25, -0.5, 2)
assert mapping.parmTuple("scale").eval() == (2, 1, 1)
assert all(not p.name().startswith("tex_mapping.") for p in texture.parms())
report["host_checks"].append(
    "Current Mapping node retains coordinates and animation through save/reload; obsolete hidden texture transforms are absent"
)
report["host_checks"].append("Disconnected nodes stay saved but do not enter the rendered graph")
report["host_checks"].append("HIP reload retains exact graph identity, socket versions, dots and animation")
for path in destination.glob("native-frame.*.exr"):
    path.unlink()
rop.parm("picture").set(str(destination / "native-frame.$F4.exr"))
rop.render(frame_range=(1, 3, 1))
for frame in (1, 2, 3):
    image = OIIO.ImageInput.open(str(destination / f"native-frame.{frame:04}.exr"))
    assert image
    channels = list(image.spec().channelnames)
    data = image.read_image(format=OIIO.FLOAT)
    actual = data[32, 32, [channels.index(c) for c in ("R", "G", "B")]]
    expected = np.array((0.2, 0.4, 0.8)) * (frame + 1)
    assert np.allclose(actual, expected, atol=1e-5), (frame, actual, expected)
    image.close()
report["host_checks"].append("Native SOHO batch evaluates the saved material independently at frames 1, 2 and 3")
report["result"] = "passed"
report["scope"] = (
    "Versioned native surface node types and selected independently checked shading semantics; subnets and ramps have separate tests; textures, volume/displacement graphs and full port certification remain release gates."
)
(destination / "houdini-material-vop-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            host_checks=len(report["host_checks"]),
            checks_per_device=[len(row["checks"]) for row in report["devices"]],
        )
    )
)
