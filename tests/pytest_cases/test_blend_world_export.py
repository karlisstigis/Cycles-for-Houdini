# SPDX-License-Identifier: Apache-2.0
"""Blender World export and simple-world classification in real Blender (bpy 5.x).

Builds one library .blend with a world per case, appends them with
blend_asset_scan.export_named_worlds (textures="in_place") and checks each rule of plan
section 10: one world that passes and one that fails it, drivers evaluated, record shapes.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu, pytest.mark.blender]

SCRIPT = r"""
import json, os, sys
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
from cycles_blender import blend_asset_scan as scan

out = sys.argv[2]
os.makedirs(out + "/library/tex", exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)


def write(path, rgba, fmt="PNG"):
    image = bpy.data.images.new("tmp", 4, 2, float_buffer=fmt != "PNG")
    image.pixels[:] = list(rgba) * 8
    image.filepath_raw = path
    image.file_format = fmt
    image.save()
    bpy.data.images.remove(image)


write(out + "/library/tex/sky.exr", (0.5, 0.6, 0.7, 1.0), "OPEN_EXR")
write(out + "/library/tex/packme.png", (0.0, 1.0, 0.0, 1.0))
sky = bpy.data.images.load(out + "/library/tex/sky.exr")
sky.filepath = "//tex/sky.exr"
packed = bpy.data.images.load(out + "/library/tex/packme.png")
packed.pack()
missing = bpy.data.images.load(out + "/library/tex/sky.exr", check_existing=False)
missing.filepath = "//tex/gone.exr"


def world(name, image=sky, mapping=True, coordinate=True):
    w = bpy.data.worlds.new(name)
    w.use_fake_user = True
    if w.node_tree is None:
        w.use_nodes = True
    tree = w.node_tree
    background = tree.nodes["Background"]
    output = tree.nodes["World Output"]
    env = tree.nodes.new("ShaderNodeTexEnvironment")
    env.image = image
    tree.links.new(env.outputs["Color"], background.inputs["Color"])
    nodes = dict(background=background, output=output, env=env, tree=tree, world=w)
    if mapping:
        nodes["mapping"] = tree.nodes.new("ShaderNodeMapping")
        tree.links.new(nodes["mapping"].outputs[0], env.inputs["Vector"])
    if coordinate:
        nodes["coord"] = tree.nodes.new("ShaderNodeTexCoord")
        target = nodes["mapping"].inputs["Vector"] if mapping else env.inputs["Vector"]
        tree.links.new(nodes["coord"].outputs["Generated"], target)
    return nodes


def multiply(name, kind):
    n = world(name)
    tree = n["tree"]
    if kind == "mix":
        node = tree.nodes.new("ShaderNodeMix")
        node.data_type, node.blend_type = "RGBA", "MULTIPLY"
        node.inputs["Factor"].default_value = 1.0
        inputs = {s.identifier: s for s in node.inputs}
        a, b, out_socket = inputs["A_Color"], inputs["B_Color"], node.outputs[2]
        b.default_value = (0.5, 0.25, 2.0, 1.0)
    elif kind == "mixrgb":
        node = tree.nodes.new("ShaderNodeMixRGB")
        node.blend_type = "MULTIPLY"
        node.inputs[0].default_value = 1.0
        b, a, out_socket = node.inputs[1], node.inputs[2], node.outputs[0]
        rgb = tree.nodes.new("ShaderNodeRGB")
        rgb.outputs[0].default_value = (0.5, 0.25, 2.0, 1.0)
        tree.links.new(rgb.outputs[0], b)
    else:
        node = tree.nodes.new("ShaderNodeVectorMath")
        node.operation = "MULTIPLY"
        a, b, out_socket = node.inputs[0], node.inputs[1], node.outputs[0]
        b.default_value = (0.5, 0.25, 2.0)
    tree.links.new(n["env"].outputs["Color"], a)
    tree.links.new(out_socket, n["background"].inputs["Color"])
    n["node"] = node
    return n


# ---- passing worlds
n = world("Simple")
n["mapping"].inputs["Rotation"].default_value = (0.0, 0.0, 0.5)
n["background"].inputs["Strength"].default_value = 2.5
world("Bare", mapping=False, coordinate=False)
world("Generated", mapping=False)
multiply("TintMix", "mix")
multiply("TintMixRGB", "mixrgb")
multiply("TintVector", "vector")
n = world("Rerouted")
tree = n["tree"]
reroute = tree.nodes.new("NodeReroute")
tree.links.new(n["env"].outputs["Color"], reroute.inputs[0])
gamma = tree.nodes.new("ShaderNodeGamma")
gamma.mute = True
tree.links.new(reroute.outputs[0], gamma.inputs[0])
tree.links.new(gamma.outputs[0], n["background"].inputs["Color"])
n = world("Driven")
w = n["world"]
w["rotation"] = 90.0
w["strength"] = 3.0
curve = n["mapping"].inputs["Rotation"].driver_add("default_value", 2)
curve.driver.type = "SCRIPTED"
variable = curve.driver.variables.new()
variable.name = "r"
variable.targets[0].id_type = "WORLD"
variable.targets[0].id = w
variable.targets[0].data_path = '["rotation"]'
curve.driver.expression = "r * pi / 180"
curve = n["background"].inputs["Strength"].driver_add("default_value")
curve.driver.type = "AVERAGE"
variable = curve.driver.variables.new()
variable.targets[0].id_type = "WORLD"
variable.targets[0].id = w
variable.targets[0].data_path = '["strength"]'
world("Packed", image=packed)
world("Missing", image=missing)

# ---- failing worlds, one per rule
n = world("EeveeOnly")
n["output"].target = "EEVEE"
n = world("Volume")
scatter = n["tree"].nodes.new("ShaderNodeVolumeScatter")
n["tree"].links.new(scatter.outputs[0], n["output"].inputs["Volume"])
n = world("Emission")
emission = n["tree"].nodes.new("ShaderNodeEmission")
n["tree"].links.new(n["env"].outputs["Color"], emission.inputs["Color"])
n["tree"].links.new(emission.outputs[0], n["output"].inputs["Surface"])
n = world("DrivenStrength")
value = n["tree"].nodes.new("ShaderNodeValue")
n["tree"].links.new(value.outputs[0], n["background"].inputs["Strength"])
n = world("ConstantColor")
n["tree"].links.remove(n["background"].inputs["Color"].links[0])
n = multiply("HalfMix", "mix")
n["node"].inputs["Factor"].default_value = 0.5
n = world("MirrorBall")
n["env"].projection = "MIRROR_BALL"
n = world("Closest")
n["env"].interpolation = "Closest"
n = world("ObjectCoordinates", mapping=False)
n["tree"].links.new(n["coord"].outputs["Object"], n["env"].inputs["Vector"])
n = world("LooseMapping", coordinate=False)
n = world("TiltedMapping")
n["mapping"].inputs["Rotation"].default_value = (0.3, 0.0, 0.5)
n = world("MovedMapping")
n["mapping"].inputs["Location"].default_value = (1.0, 0.0, 0.0)
n = world("ScaledMapping")
n["mapping"].inputs["Scale"].default_value = (2.0, 2.0, 2.0)
n = world("TextureMapping")
n["mapping"].vector_type = "TEXTURE"
n = world("Hidden")
n["world"].cycles_visibility.camera = False
n = world("Several")
n["env"].projection = "MIRROR_BALL"
n["world"].cycles_visibility.glossy = False
bpy.ops.wm.save_as_mainfile(filepath=out + "/library/worlds.blend")
names = [w.name for w in bpy.data.worlds]

bpy.ops.wm.read_factory_settings(use_empty=True)
result = scan.export_named_worlds(out + "/library/worlds.blend", names + ["Nope"], out + "/stage")
staged = sorted(
    os.path.relpath(os.path.join(p, f), out + "/stage").replace(os.sep, "/") for p, _, fs in os.walk(out + "/stage") for f in fs
)
print("RESULT:" + json.dumps(dict(result, staged=staged, library=out + "/library", worlds_left=[w.name for w in bpy.data.worlds])))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_world_export")
    script = folder / "make_worlds.py"
    script.write_text(SCRIPT, encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(root / "houdini/python3.13libs"), str(folder / "out")],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    result = json.loads(lines[-1][len("RESULT:") :])
    result["by_name"] = {record["name"]: record for record in result["records"]}
    return result


def _posix(path):
    return str(Path(path).resolve()).replace("\\", "/")


SIMPLE = {
    # name: (strength, tint, Mapping Z rotation)
    "Simple": (2.5, [1.0, 1.0, 1.0], 0.5),
    "Bare": (1.0, [1.0, 1.0, 1.0], 0.0),
    "Generated": (1.0, [1.0, 1.0, 1.0], 0.0),
    "TintMix": (1.0, [0.5, 0.25, 2.0], 0.0),
    "TintMixRGB": (1.0, [0.5, 0.25, 2.0], 0.0),
    "TintVector": (1.0, [0.5, 0.25, 2.0], 0.0),
    "Rerouted": (1.0, [1.0, 1.0, 1.0], 0.0),
    "Driven": (3.0, [1.0, 1.0, 1.0], 1.5707963),
}
FAILING = {
    # name: (failed rules, kind)
    "EeveeOnly": (["output"], "unsupported"),
    "Volume": (["volume"], "material"),
    "Emission": (["background"], "material"),
    "DrivenStrength": (["strength"], "material"),
    "ConstantColor": (["color"], "material"),
    "HalfMix": (["color"], "material"),
    "MirrorBall": (["texture"], "material"),
    "Closest": (["texture"], "material"),
    "ObjectCoordinates": (["vector"], "material"),
    "LooseMapping": (["vector"], "material"),
    "TiltedMapping": (["mapping"], "material"),
    "MovedMapping": (["mapping"], "material"),
    "ScaledMapping": (["mapping"], "material"),
    "TextureMapping": (["mapping"], "material"),
    "Hidden": (["visibility"], "material"),
    "Several": (["visibility", "texture"], "material"),
}


@pytest.mark.parametrize("name", sorted(SIMPLE))
def test_simple_worlds_pass_every_rule(exported, name):
    record = exported["by_name"][name]
    assert (record["kind"], record["reasons"], record["warnings"]) == ("simple", [], [])
    strength, tint, rotation = SIMPLE[name]
    simple = record["simple"]
    assert simple["strength"] == pytest.approx(strength)
    assert simple["tint"] == pytest.approx(tint)
    assert simple["rotation_z"] == pytest.approx(rotation, abs=1e-6)
    image = simple["image"]
    assert image["filename"] == _posix(exported["library"]) + "/tex/sky.exr"
    assert (image["textures"], image["packed"], image["missing"]) == ("in_place", False, False)
    assert set(record["visibility"].values()) == {True}
    assert record["key"].startswith("blender-world-") and record["color_management"]["colorspace"]


@pytest.mark.parametrize("name", sorted(FAILING))
def test_each_failed_rule_is_named(exported, name):
    record = exported["by_name"][name]
    rules, kind = FAILING[name]
    assert [reason["rule"] for reason in record["reasons"]] == rules
    assert all(reason["message"] for reason in record["reasons"])
    assert record["kind"] == kind and "simple" not in record
    if kind == "material":
        material = record["material"]
        assert material["name"] == name and material["outputs"]["surface"]["source"]
    else:
        assert "World Output" in record["unsupported"]


def test_failing_world_graph_is_exported_for_the_world_material(exported):
    material = exported["by_name"]["TiltedMapping"]["material"]
    types = [node["type"] for node in material["nodes"]]
    assert types == ["ShaderNodeBackground", "ShaderNodeTexEnvironment", "ShaderNodeMapping", "ShaderNodeTexCoord"]
    assert material["nodes"][1]["image"]["filename"].endswith("/tex/sky.exr")
    assert set(exported["by_name"]["Volume"]["material"]["outputs"]) == {"surface", "volume"}
    assert exported["by_name"]["Hidden"]["visibility"]["camera"] is False


def test_packed_and_missing_hdris(exported):
    packed = exported["by_name"]["Packed"]["simple"]["image"]
    assert (packed["filename"], packed["packed"]) == ("textures/packme.png", True)
    assert len(packed["dependencies"][0]["sha256"]) == 64
    assert exported["staged"] == ["textures/packme.png"]  # nothing else read or copied
    missing = exported["by_name"]["Missing"]
    assert missing["kind"] == "simple" and missing["simple"]["image"]["missing"] is True
    assert missing["simple"]["image"]["filename"] == _posix(exported["library"]) + "/tex/gone.exr"


def test_unknown_names_are_errors_and_appended_data_is_removed(exported):
    assert list(exported["errors"]) == ["Nope"] and "not found" in exported["errors"]["Nope"]
    assert exported["worlds_left"] == []


def test_blender_orientation_reference_shows_marks_opposite_to_the_mapping(tmp_path):
    """Blender half of the orientation gate: mark m shows in the direction R_z(-z) m."""
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    script = Path(__file__).resolve().parents[1] / "fixtures/blend_worlds/orientation_reference.py"
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=tmp_path,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    report = json.loads(lines[-1][len("RESULT:") :])
    seen = {
        (angle, mark): probe["seen"] for angle, probes in report["angles"].items() for mark, probe in probes.items()
    }
    assert report["ok"] and all(angle_mark[1] == mark for angle_mark, mark in seen.items()), seen
    assert sorted(report["angles"]) == ["0", "215", "90"]
    assert (tmp_path / "out/blender_215_mirror_ball.exr").is_file()
