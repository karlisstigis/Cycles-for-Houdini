# SPDX-License-Identifier: Apache-2.0
"""One modern Mix, Blender names, dormant inputs, animation, repair and pixels."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import cycles_houdini as ch
from mix_nodes_fixture import Fixture, KINDS, PRIVATE_KINDS, A, B, check_layout

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/mix-nodes"
dest.mkdir(parents=True, exist_ok=True)
checks = []
old_hip = artifacts_root() / "mix-modern-before.hiplc"
for kind in PRIVATE_KINDS:
    assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::" + kind + "::1") is None, kind
if old_hip.exists():
    hou.hipFile.load(str(old_hip), suppress_save_prompt=True, ignore_load_warnings=True)
    builder = hou.node("/mat/mix_saved_wiring")
    for kind in ("mix_closure", "mix_color"):
        node = builder.node("saved_" + kind)
        check_layout(node)
        assert node.input(node.inputIndex("fac")) == builder.node("old_factor")
        assert [node.input(i).name() for i in (0, 1)] == (
            ["old_shader_a", "old_shader_b"] if kind == "mix_closure" else ["old_a", "old_b"]
        )
    checks.append(dict(name="Existing modern Mix and Mix Shader named HIP wiring retained", nodes=2))

f = Fixture(device)
first = True
conversions = None
for kind, stem in (("mix_closure", "mix_shader"), ("add_closure", "add_shader"), ("mix_color", "mix")):
    automatic = f.builder.createNode("cycles::" + kind + "::1")
    assert automatic.name().startswith(stem), automatic.name()
    if kind == "mix_color":
        assert automatic.evalParm("data_type") == "color" and automatic.evalParm("fac") == 1
        assert automatic.parmTuple("a").eval() == automatic.parmTuple("b").eval() == (0.5, 0.5, 0.5)
        assert automatic.evalParm("a_float") == automatic.evalParm("b_float") == 0
        assert automatic.parmTuple("a_vector").eval() == automatic.parmTuple("b_vector").eval() == (0, 0, 0)
        assert automatic.parmTuple("fac_vector").eval() == (0.5, 0.5, 0.5)
        assert automatic.evalParm("use_clamp") == 1 and automatic.evalParm("use_clamp_result") == 0
        checks.append(dict(name="New Mix uses Blender Color-menu authoring defaults"))
    automatic.destroy()
assert sum(t.description() == "Cycles Mix" for t in hou.vopNodeTypeCategory().nodeTypes().values()) == 1
with ch.Session(
    device=device, width=64, height=64, samples=8, threads=2, collect_diagnostics=True, use_texture_cache=False
) as session:

    def render(expected, name, warning=False, at_time=None):
        global first, conversions
        graph = ch.material_graph(f.builder, at_time=at_time)
        update = dict(materials=[dict(key="surface", kind="graph", graph=graph)])
        if first:
            update.update(
                geometry=[ch.geometry_request(f.geo, f.sop, material="surface")],
                camera_path=f.camera.path(),
                background=(0, 0, 0),
            )
            first = False
        frame = session.wait(session.apply(update, at_time=at_time))
        status = session.status()
        assert status["state"] == "complete" and not status["error"], status
        assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
        assert status["render_device_ids"] == [device]
        if conversions is None:
            conversions = status["geometry_conversions"]
        assert status["geometry_conversions"] == conversions
        checks.append(
            dict(
                name=name,
                measured=f.expect(frame, expected) if expected is not None else f.measure(frame).tolist(),
                diagnostics=status["source_diagnostics"],
            )
        )
        return graph

    for kind in KINDS:
        for factor in (0.25, 0.75):
            expected = f.select(kind, factor)
            check_layout(f.nodes[kind])
            graph = render(expected, kind + " linked Factor " + str(factor))
            engine = (
                "mix_closure"
                if kind == "mix_closure"
                else "mix_color"
                if kind == "color"
                else "mix_float"
                if kind == "float"
                else "mix_vector_non_uniform"
                if kind == "vector_non_uniform"
                else "mix_vector"
            )
            assert any(n["definition"] == "cycles/" + engine + "/1" for n in graph["nodes"]), graph
        node = f.nodes[kind]
        node.bypass(True)
        render((0.2,) * 3 if kind == "float" else A, kind + " bypass uses its active A input")
        node.bypass(False)
    modern = f.mix
    f.select("color", 2)
    render(B, "Color Clamp Factor defaults on")
    modern.parm("use_clamp").set(False)
    render(2 * B - A, "Unclamped Factor extrapolates")
    modern.parm("use_clamp_result").set(True)
    render((1, 1, 1), "Clamp Result independently limits color")
    modern.parm("use_clamp").set(True)
    render((0.6, 0.8, 1), "Factor and result clamps are independent")
    modern.parm("use_clamp_result").set(False)
    f.select("vector_non_uniform")
    f.vector_factor.parmTuple("value").set((0, 0.5, 1))
    render((A[0], (A[1] + B[1]) / 2, B[2]), "Non-Uniform factor mixes XYZ independently")
    f.select("float")
    modern.setInput(modern.inputIndex("fac"), None)
    modern.parm("fac").set(0.5)
    modern.updateParmStates()
    assert not modern.parm("fac").isDisabled()
    render((0.4,) * 3, "Disconnect restores editable scalar Factor")
    modern.setNamedInput("fac", f.factor, "value")
    f.factor.parm("value").set(0.75)
    render((0.5,) * 3, "Reconnect restores linked Factor")
    modern.parmTuple("b").set((float("nan"),) * 3)
    render((0.5,) * 3, "Float ignores broken dormant color inputs")
    f.select("color")
    modern.setNamedInput("a", f.texture, "color")
    render(None, "Color exposes its broken image and local recovery", True)
    f.texture.parm("filename").set(f.image((*A, 1)))
    modern.parmTuple("b").set(tuple(B))
    modern.setInput(modern.inputIndex("b"), None)
    render(A * 0.75 + B * 0.25, "Repair restores Color without restarting the session")
    modern.setNamedInput("a", f.a, "color")
    modern.setNamedInput("b", f.b, "color")
    # Gray numeric wires are intentionally unchanged while Data Type animates.
    gray_a = f.builder.createNode("cycles::value::1")
    gray_a.parm("value").set(0.2)
    gray_b = f.builder.createNode("cycles::value::1")
    gray_b.parm("value").set(0.6)
    modern.setNamedInput("a", gray_a, "value")
    modern.setNamedInput("b", gray_b, "value")
    hou.setFrame(1)
    modern.parm("data_type").setExpression('ifs($F<2,"float","color")', hou.exprLanguage.Hscript)
    graph = render((0.3,) * 3, "Animated Data Type at visible frame one", at_time=hou.frameToTime(1))
    assert any(n["definition"] == "cycles/mix_float/1" for n in graph["nodes"])
    graph = render((0.3,) * 3, "Explicit frame two selects Color without moving timeline", at_time=hou.frameToTime(2))
    assert hou.frame() == 1 and any(n["definition"] == "cycles/mix_color/1" for n in graph["nodes"])
    modern.bypass(True)
    render((0.2,) * 3, "Bypass selects Color A at explicit render time", at_time=hou.frameToTime(2))
    modern.bypass(False)
    modern.parm("data_type").deleteAllKeyframes()
    f.select("color")
    original = modern.color()
    modern.setColor(hou.Color((0.9, 0.3, 0.1)))
    f.select("float")
    assert modern.color().rgb() == hou.Color((0.9, 0.3, 0.1)).rgb()
    f.select("color")
    modern.setColor(original)
    added = f.builder.createNode("cycles::add_closure::1", "add_shader")
    assert added.type().description() == "Cycles Add Shader"
    added.setNamedInput("closure1", f.shader_a, "emission")
    added.setNamedInput("closure2", f.shader_b, "emission")
    f.output.setNamedInput("surface", added, "closure")
    render(A + B, "Add Shader retains its Blender name and semantics")

reference = json.loads((root / "tests/fixtures/blender-shader-node-names.json").read_text())
names = []
world = ch.create_world_material(name="name_reference_world")
for kind, item in reference["nodes"].items():
    host = world if kind in ("background_shader", "world_output") else f.builder
    node = host.createNode("cycles::" + kind + "::1")
    assert node.type().description() == "Cycles " + item["label"], (kind, node.type().description(), item)
    names.append(kind)
    node.destroy()
path = f.builder.path()
hip = dest / "mix-nodes.hiplc"
hou.hipFile.save(str(hip))
hou.hipFile.load(str(hip), suppress_save_prompt=True, ignore_load_warnings=True)
modern = hou.node(path).node("mix")
for kind in ("color", "float", "vector"):
    modern.parm("data_type").set(kind)
    check_layout(modern)
assert modern.input(modern.inputIndex("a")) is not None and modern.inputNames() == ("a", "b", "fac")
report = dict(
    result="passed",
    device=device,
    checks=checks,
    blender_names=names,
    private_native_types_absent=list(PRIVATE_KINDS),
    hip=str(hip),
    scope="One native modern Mix with typed lowering, active sockets/parameters, dormant branch repair, requested-time animation, bypass and independent pixels. Blender names audited separately from engine IDs; GUI verified separately.",
)
(dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks), blender_names=len(names))))
