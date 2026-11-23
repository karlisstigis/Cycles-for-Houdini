# SPDX-License-Identifier: Apache-2.0
"""Blender World import on the host (blend-import.md: Worlds), pure and with a fake `hou`.

Covers the orientation mapping (checked against scene/world.py's own environment
sampling), the Environment Light parameters, Link reuse and Append naming, packed and
missing HDRIs, the World Material translation of world graphs, the Replace / Keep plans
for the existing environment, reload of a linked world, and the worker command.
"""

import contextlib
import hashlib
import json
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from cycles_houdini.blender import blend_convert, blend_library_materials, blend_materials, blend_worlds

import test_blend_material_axes as axes

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
world = axes.world  # fake /mat network for the translator (fixture)

ANGLES = (0.0, 90.0, 215.0)


# ---------------------------------------------------------------- orientation gate (automated half)


class _Vector3:
    def __init__(self, *values):
        self.a = np.asarray(values[0] if len(values) == 1 else values, dtype=np.float64)

    def __mul__(self, matrix):  # Houdini row vector times matrix
        return _Vector3(self.a @ matrix.a)

    def __neg__(self):
        return _Vector3(-self.a)

    def __iter__(self):
        return iter(float(v) for v in self.a)

    def length(self):
        return float(np.linalg.norm(self.a))

    def normalized(self):
        return _Vector3(self.a / np.linalg.norm(self.a))

    def dot(self, other):
        return float(self.a @ other.a)


class _Matrix3:
    def __init__(self, matrix):
        self.a = np.asarray(matrix, dtype=np.float64)[:3, :3]


def houdini_rotation(rx, ry, rz):
    """Houdini's object rotation (row vectors, rotate order xyz, right-handed), 4x4."""

    def row(axis, degrees):
        c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        i, j = [(1, 2), (2, 0), (0, 1)][axis]
        m = np.eye(3)
        m[i, i], m[i, j], m[j, i], m[j, j] = c, s, -s, c
        return m

    matrix = np.eye(4)
    matrix[:3, :3] = row(0, rx) @ row(1, ry) @ row(2, rz)
    return matrix


def houdini_samples(rotation, monkeypatch):
    """The 3x3 map d_houdini -> Cycles lookup coordinates that scene/world.py builds for an
    Environment Light with this object rotation (read from its own axis_x/y/z nodes)."""
    monkeypatch.setitem(sys.modules, "hou", sys.modules.get("hou") or ModuleType("hou"))
    from cycles_houdini.scene import world as world_module

    fake = SimpleNamespace(Vector3=_Vector3, Matrix3=_Matrix3, expandString=lambda text: text)
    monkeypatch.setattr(world_module, "hou", fake)
    parms = {"env_map": "/hdri/marked.exr"}
    node = SimpleNamespace(
        parm=lambda name: SimpleNamespace(evalAtTime=lambda now: parms[name]) if name in parms else None,
        parmTuple=lambda name: SimpleNamespace(evalAtTime=lambda now: (1.0, 1.0, 1.0)),
        worldTransformAtTime=lambda now: houdini_rotation(*rotation),
        path=lambda: "/obj/env",
    )
    graph = world_module.native_environment(node, 0.0)["world"]["graph"]
    rows = {item["id"]: item["parameters"]["vector2"] for item in graph["nodes"] if item["id"].startswith("axis_")}
    return np.array([rows["axis_x"], rows["axis_y"], rows["axis_z"]])


def equirect(direction):
    """Cycles' direction_to_equirectangular (default range): (u, v)."""
    x, y, z = direction / np.linalg.norm(direction)
    return (-math.atan2(y, x) / (2 * math.pi) + 0.5) % 1.0, math.acos(max(-1.0, min(1.0, z))) / math.pi


@pytest.mark.parametrize("angle", ANGLES)
def test_rotation_shows_blender_marks_in_the_converted_directions(angle, monkeypatch):
    z = math.radians(angle)
    rotation = blend_worlds.light_rotation_for_blender_mapping(z)
    houdini = houdini_samples(rotation, monkeypatch)
    convert = blend_convert._CONVERT[:3, :3]  # Blender (x, y, z) -> Houdini (x, z, -y)
    c, s = math.cos(z), math.sin(z)
    mapping = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])  # Mapping (Point), rotation Z
    marks = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0), "zenith": (0, 0, 1)}
    for label, blender_direction in marks.items():
        blender_direction = np.array(blender_direction, dtype=np.float64)
        blender_pixel = equirect(mapping @ blender_direction)  # what Blender shows there
        houdini_pixel = equirect(houdini @ (convert @ blender_direction))  # same direction, converted axes
        assert houdini_pixel == pytest.approx(blender_pixel, abs=1e-9), (angle, label)


def test_rotation_formula_values():
    assert blend_worlds.light_rotation_for_blender_mapping(0.0) == (0.0, -90.0, 0.0)
    assert blend_worlds.light_rotation_for_blender_mapping(math.radians(90)) == pytest.approx((0.0, -180.0, 0.0))
    assert blend_worlds.light_rotation_for_blender_mapping(math.radians(215)) == pytest.approx((0.0, 55.0, 0.0))
    assert blend_worlds.light_rotation_for_blender_mapping(math.radians(-90)) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------- light parameters (pure)


def test_light_parameters_and_colour_space():
    matrix = [[0.5, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 1.0]]
    simple = dict(strength=2.5, tint=[1.0, 0.5, 0.25], rotation_z=math.radians(215))
    parameters = blend_worlds.light_parameters(
        simple, env_map="C:/hdri/sky.exr", colorspace="ACEScg", to_working=matrix
    )
    assert parameters == {
        "env_map": "C:/hdri/sky.exr",
        "light_intensity": 2.5,
        "light_exposure": 0.0,
        "light_color": (0.5, 1.0, 0.25),
        "cycles_environment_colorspace": "ACEScg",
        "light_contribprimary": 1,
        "t": (0.0, 0.0, 0.0),
        "r": pytest.approx((0.0, 55.0, 0.0)),
    }

    def find(config, image):
        if not image.get("color_interop_id"):
            raise blend_materials.BlendMaterialError("no interop identity")
        return "Linear Rec.709 (sRGB)"

    assert blend_worlds.light_colorspace(dict(is_data=True), None, find) == ("data", None)
    assert blend_worlds.light_colorspace(dict(color_interop_id="lin_rec709_scene"), None, find) == (
        "Linear Rec.709 (sRGB)",
        None,
    )
    space, warning = blend_worlds.light_colorspace(dict(colorspace="Custom"), None, find)
    assert space == "auto" and "Auto" in warning


# ---------------------------------------------------------------- existing environment (pure)


def rop(path="/out/cycles", source="scene", material="", lights=()):
    return dict(path=path, world_source=source, world_material=material, lights=list(lights))


def test_empty_scene_activates_the_new_world():
    state = blend_worlds.environment_state({"rops": [], "lights": []})
    assert (state["active"], state["kind"]) == ([], "none")
    for choice in ("replace", "keep"):  # nothing to keep: keep acts as replace
        plan = blend_worlds.activation_plan("light", state, choice, new_path="/obj/Sky")
        assert plan == {"enable": ["/obj/Sky"], "disable": [], "rops": [], "report": []}
    plan = blend_worlds.activation_plan("material", state, "replace", new_path="/mat/Sky")
    assert plan["rops"] == [] and "No Cycles ROP" in plan["report"][0]


def test_one_enabled_light_in_scene_mode():
    facts = {"rops": [rop(lights=["/obj/env1"])], "lights": ["/obj/env1"]}
    state = blend_worlds.environment_state(facts)
    assert (state["active"], state["kind"]) == (["/obj/env1"], "light")
    plan = blend_worlds.activation_plan("light", state, "replace", new_path="/obj/Sky")
    assert (plan["enable"], plan["disable"], plan["rops"]) == (["/obj/Sky"], ["/obj/env1"], [])
    keep = blend_worlds.activation_plan("light", state, "keep", new_path="/obj/Sky")
    assert (keep["enable"], keep["disable"], keep["rops"]) == ([], ["/obj/Sky"], [])
    material = blend_worlds.activation_plan("material", state, "replace", new_path="/mat/Sky")
    assert material["disable"] == [] and material["rops"] == [
        {"path": "/out/cycles", "set": {"world_source": "material", "world_material": "/mat/Sky"}}
    ]
    kept = blend_worlds.activation_plan("material", state, "keep", new_path="/mat/Sky")
    assert (kept["enable"], kept["disable"], kept["rops"]) == ([], [], [])


def test_rop_in_material_mode():
    facts = {"rops": [rop(source="material", material="/mat/old")], "lights": ["/obj/env1"]}
    state = blend_worlds.environment_state(facts)
    assert (state["active"], state["kind"]) == (["/mat/old"], "material")
    plan = blend_worlds.activation_plan("light", state, "replace", new_path="/obj/Sky")
    assert plan["rops"] == [{"path": "/out/cycles", "set": {"world_source": "scene"}}]
    assert plan["disable"] == ["/obj/env1"] and any("World Source set to Scene" in line for line in plan["report"])
    # A material mode ROP with an empty World Material has no environment lighting.
    state = blend_worlds.environment_state({"rops": [rop(source="material")], "lights": []})
    assert state["active"] == []


def test_two_rops():
    facts = {
        "rops": [rop("/out/a", lights=["/obj/env1"]), rop("/out/b", source="material", material="/mat/old")],
        "lights": ["/obj/env1", "/obj/env2"],
    }
    state = blend_worlds.environment_state(facts)
    assert (state["active"], state["kind"]) == (["/obj/env1", "/mat/old"], "mixed")
    plan = blend_worlds.activation_plan("light", state, "replace", new_path="/obj/Sky")
    assert plan["disable"] == ["/obj/env1", "/obj/env2"]
    assert plan["rops"] == [{"path": "/out/b", "set": {"world_source": "scene"}}]
    plan = blend_worlds.activation_plan("material", state, "replace", new_path="/mat/Sky")
    assert plan["rops"] == [
        {"path": "/out/a", "set": {"world_source": "material", "world_material": "/mat/Sky"}},
        {"path": "/out/b", "set": {"world_material": "/mat/Sky"}},
    ]
    with pytest.raises(ValueError, match="replace"):
        blend_worlds.activation_plan("light", state, "add")


def test_lights_without_rop_count_as_active():
    state = blend_worlds.environment_state({"rops": [], "lights": ["/obj/env1"]})
    assert (state["active"], state["kind"]) == (["/obj/env1"], "light")


# ---------------------------------------------------------------- fake hou scene


class Parm:
    def __init__(self, node, name, value):
        self.node, self.name, self.value, self.locked = node, name, value, False

    def set(self, value):
        if self.locked:
            raise PermissionError("locked parameter: " + self.path())
        self.value = value

    def eval(self):
        return self.value

    def evalAsString(self):
        return str(self.value)

    def isLocked(self):
        return self.locked

    def lock(self, on):
        self.locked = bool(on)

    def path(self):
        return self.node.path() + "/" + self.name


DEFAULTS = {
    "envlight": dict(
        env_map="",
        light_intensity=1.0,
        light_exposure=0.0,
        light_color=(1.0, 1.0, 1.0),
        light_enable=1,
        light_contribprimary=0,
        cycles_environment_colorspace="auto",
        t=(0.0, 0.0, 0.0),
        r=(0.0, 0.0, 0.0),
    ),
    "cycles_ipr": dict(world_source="scene", world_material=""),
}


class HNode:
    def __init__(self, scene, parent, type_name, name):
        self.scene, self._parent, self.type_name, self._name = scene, parent, type_name, name
        self.kids, self.user, self.comment, self.colour = [], {}, "", None
        self._parms, self._tuples = {}, {}
        for key, value in DEFAULTS.get(type_name, {}).items():
            if isinstance(value, tuple):
                self._tuples[key] = tuple(Parm(self, key + "xyz"[i], v) for i, v in enumerate(value))
            else:
                self._parms[key] = Parm(self, key, value)

    def path(self):
        return (self._parent.path() if self._parent and self._parent._parent else "") + "/" + self._name

    def name(self):
        return self._name

    def type(self):
        return SimpleNamespace(name=lambda: self.type_name)

    def createNode(self, type_name, name=None):
        name = name or type_name + "1"
        base, index = name, 1
        while any(k._name == name for k in self.kids):
            name, index = base + str(index), index + 1
        child = HNode(self.scene, self, type_name, name)
        self.kids.append(child)
        return child

    def children(self):
        return list(self.kids)

    def allSubChildren(self):
        return [n for k in self.kids for n in [k, *k.allSubChildren()]]

    def node(self, path):
        return self.scene.node(path if path.startswith("/") else self.path() + "/" + path)

    def parm(self, name):
        return self._parms.get(name)

    def parmTuple(self, name):
        return self._tuples.get(name)

    def parms(self):
        return list(self._parms.values()) + [p for t in self._tuples.values() for p in t]

    def values(self):
        return {
            **{k: p.value for k, p in self._parms.items()},
            **{k: tuple(p.value for p in t) for k, t in self._tuples.items()},
        }

    def setName(self, name, unique_name=False):
        self._name = name

    def setUserData(self, key, value):
        self.user[key] = value

    def userData(self, key):
        return self.user.get(key)

    def destroyUserData(self, key):
        self.user.pop(key, None)

    def setComment(self, text):
        self.comment = text

    def setColor(self, colour):
        self.colour = colour

    def color(self):
        return self.colour

    def setGenericFlag(self, flag, on):
        pass

    def moveToGoodPosition(self):
        pass

    def setCurrent(self, on, clear_all_selected=False):
        pass

    def position(self):
        return (0.0, 0.0)

    def setPosition(self, position):
        pass

    def destroy(self):
        self._parent.kids.remove(self)


class Scene:
    def __init__(self):
        self.root = HNode(self, None, "root", "")
        self.obj, self.mat, self.out = (
            self.root.createNode(t, n) for t, n in (("obj", "obj"), ("mat", "mat"), ("out", "out"))
        )
        self.undo = []

    def node(self, path):
        for node in [self.root, *self.root.allSubChildren()]:
            if node.path() == path:
                return node
        return None

    def rop(self, name, **values):
        node = self.out.createNode("cycles_ipr", name)
        for key, value in values.items():
            node.parm(key).value = value
        return node

    def light(self, name, enabled=True):
        node = self.obj.createNode("envlight", name)
        node.parm("light_enable").value = int(enabled)
        return node


@pytest.fixture
def scene(monkeypatch):
    scene = Scene()

    @contextlib.contextmanager
    def group(label):
        scene.undo.append(label)
        yield

    rop_type = SimpleNamespace(instances=lambda: [n for n in scene.out.children() if n.type_name == "cycles_ipr"])
    hou = SimpleNamespace(
        node=scene.node,
        undos=SimpleNamespace(group=group),
        nodeType=lambda category, name: rop_type if name == "cycles_ipr" else None,
        ropNodeTypeCategory=lambda: "rop",
        time=lambda: 0.0,
        text=SimpleNamespace(expandString=lambda text: text),
        Color=lambda rgb: SimpleNamespace(rgb=lambda: tuple(rgb)),
        nodeFlag=SimpleNamespace(DisplayComment="comment"),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    # Every enabled light is selected (ROP Lights "*"); scene/world.py does the real selection.
    monkeypatch.setattr(
        blend_worlds,
        "_selected_lights",
        lambda rop: [n for n in blend_worlds._environment_lights() if blend_worlds._enabled(n)],
    )
    monkeypatch.setattr(blend_worlds, "_install", lambda node: None)
    monkeypatch.setattr(blend_worlds, "_source_signature", lambda source: [1, 2, 3, 4])
    matrix = [[0.5, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 2.0]]
    monkeypatch.setattr(blend_materials, "_color_matrices", lambda metadata: ("config", matrix, matrix))
    monkeypatch.setattr(blend_materials, "_find_space", lambda config, image: "Linear Rec.709 (sRGB)")
    return scene


def image_record(path, *, missing=False):
    return dict(
        filename=path,
        source_path=path,
        source_kind="FILE",
        colorspace="Linear Rec.709",
        color_interop_id="lin_rec709_scene",
        is_data=False,
        alpha_mode="STRAIGHT",
        tiles=[],
        dependencies=[dict(filename=path, source_path=path, packed=False, missing=missing)],
        textures="in_place",
        packed=False,
        missing=missing,
    )


def simple_record(name="Sky", image=None, rotation=215.0):
    return dict(
        name=name,
        key="blender-world-0123456789abcdef01234567",
        kind="simple",
        reasons=[],
        warnings=[],
        visibility={},
        color_management=dict(colorspace="Linear Rec.709", color_interop_id="lin_rec709_scene"),
        simple=dict(
            strength=2.5,
            tint=[1.0, 1.0, 0.5],
            rotation_z=math.radians(rotation),
            image=image or image_record("D:/Library/hdri/sky_4k.exr"),
        ),
    )


def prepared(tmp_path, record=None, source="D:/Library/hdri/sky.blend"):
    record = record or simple_record()
    return dict(
        record=record,
        route=blend_worlds.ROUTES[record["kind"]],
        root=str(tmp_path / "assets_blender/sky/tex"),
        source=source,
        name=record["name"],
        asset_root=str(tmp_path / "assets_blender"),
        blend_folder=str(tmp_path / "assets_blender/sky"),
    )


# ---------------------------------------------------------------- build: simple world -> Environment Light


def test_simple_world_becomes_an_environment_light(scene, tmp_path):
    diagnostics = []
    result = blend_worlds.build_world(prepared(tmp_path), mode="append", diagnostics=diagnostics)
    node = result["node"]
    assert (node.path(), node.type_name, result["reused"], result["route"]) == ("/obj/Sky", "envlight", False, "light")
    values = node.values()
    assert values["env_map"] == "D:/Library/hdri/sky_4k.exr"
    assert (values["light_intensity"], values["light_exposure"], values["light_contribprimary"]) == (2.5, 0.0, 1)
    assert values["light_color"] == (0.5, 1.0, 1.0)  # tint through the working-space matrix
    assert values["cycles_environment_colorspace"] == "Linear Rec.709 (sRGB)"
    assert values["r"] == pytest.approx((0.0, 55.0, 0.0)) and values["t"] == (0.0, 0.0, 0.0)
    assert values["light_enable"] == 1 and diagnostics == []
    assert json.loads(node.user[blend_worlds.WORLD_USER_DATA]) == {
        "source": "D:/Library/hdri/sky.blend",
        "name": "Sky",
        "route": "light",
    }
    assert any("stays at D:/Library/hdri/sky_4k.exr" in line for line in result["report"])
    assert scene.undo == ["Import Blender world Sky"]
    assert not any(p.locked for p in node.parms())  # Append: editable


def test_append_twice_creates_numbered_copies(scene, tmp_path):
    first = blend_worlds.build_world(prepared(tmp_path), mode="append")["node"]
    second = blend_worlds.build_world(prepared(tmp_path), mode="append")["node"]
    assert (first.name(), second.name()) == ("Sky", "Sky_1")
    # Replace (default): the new one is the active one, the first is disabled, not deleted.
    assert (first.parm("light_enable").value, second.parm("light_enable").value) == (0, 1)


def test_link_builds_one_locked_shared_light_and_reuses_it(scene, tmp_path):
    first = blend_worlds.build_world(prepared(tmp_path), mode="link", link_info={"library": "Poly Haven"})
    node = first["node"]
    info = blend_library_materials.link_info(node)
    assert (info["kind"], info["route"], info["name"], info["library"]) == ("world", "light", "Sky", "Poly Haven")
    assert info["signature"] == [1, 2, 3, 4]
    assert node.comment == "Linked from sky.blend: Sky" and node.colour.rgb() == blend_library_materials.LINKED_COLOR
    locked = {p.name for p in node.parms() if p.locked}
    assert "env_map" in locked and "rx" in locked and "light_enable" not in locked
    again = blend_worlds.build_world(prepared(tmp_path), mode="link")
    assert again["node"] is node and again["reused"] is True
    assert [n.name() for n in scene.obj.children()] == ["Sky"]
    assert blend_worlds.find_linked_world("D:/Library/hdri/sky.blend", "Sky") is node
    assert blend_worlds.existing_environment("D:/Library/hdri/sky.blend", "Sky") == []
    assert blend_worlds.existing_environment("D:/Library/hdri/other.blend", "Sky") == ["/obj/Sky"]
    # Make Editable (the materials' unmark_linked) works on the light as well.
    assert blend_library_materials.unmark_linked(node) and not any(p.locked for p in node.parms())


def test_missing_hdri_warns_and_still_creates_the_light(scene, tmp_path):
    record = simple_record(image=image_record("D:/Library/hdri/gone.exr", missing=True))
    diagnostics = []
    result = blend_worlds.build_world(prepared(tmp_path, record), mode="append", diagnostics=diagnostics)
    assert result["node"].parm("env_map").value == "D:/Library/hdri/gone.exr"
    (warning,) = diagnostics
    assert (warning["code"], warning["severity"], warning["path"]) == (
        "material.missing_texture",
        "warning",
        "D:/Library/hdri/gone.exr",
    )
    assert warning["message"] in result["report"]


def test_unresolved_colour_space_falls_back_to_auto_with_a_warning(scene, tmp_path, monkeypatch):
    def unresolved(config, image):
        raise blend_materials.BlendMaterialError("no interop identity")

    monkeypatch.setattr(blend_materials, "_find_space", unresolved)
    diagnostics = []
    node = blend_worlds.build_world(prepared(tmp_path), mode="append", diagnostics=diagnostics)["node"]
    assert node.parm("cycles_environment_colorspace").value == "auto"
    assert [d["code"] for d in diagnostics] == ["world.colorspace"]


# ---------------------------------------------------------------- build: existing environment (blend-import.md: Worlds)


def test_replace_disables_existing_light_and_switches_material_rop(scene, tmp_path):
    old = scene.light("envlight1")
    rop = scene.rop("cycles", world_source="material", world_material="/mat/old")
    scene.mat.createNode("cycles::world_material::1", "old")
    assert blend_worlds.existing_environment() == ["/mat/old"]
    result = blend_worlds.build_world(prepared(tmp_path), mode="append")
    assert old.parm("light_enable").value == 0 and old in scene.obj.children()
    assert rop.parm("world_source").value == "scene" and rop.parm("world_material").value == "/mat/old"
    assert any("envlight1" in line for line in result["report"])


def test_keep_imports_the_light_disabled_and_changes_nothing_else(scene, tmp_path):
    old = scene.light("envlight1")
    rop = scene.rop("cycles")
    before = (old.values(), rop.values())
    assert blend_worlds.existing_environment() == ["/obj/envlight1"]
    result = blend_worlds.build_world(prepared(tmp_path), mode="append", existing_environment="keep")
    assert result["node"].parm("light_enable").value == 0
    assert (old.values(), rop.values()) == before
    assert any("Kept the existing environment" in line for line in result["report"])


def test_reused_linked_light_is_enabled_again_on_replace(scene, tmp_path):
    node = blend_worlds.build_world(prepared(tmp_path), mode="link")["node"]
    other = scene.light("envlight1")
    blend_worlds.build_world(prepared(tmp_path), mode="link")
    assert (node.parm("light_enable").value, other.parm("light_enable").value) == (1, 0)
    with pytest.raises(ValueError, match="link"):
        blend_worlds.build_world(prepared(tmp_path), mode="reference")
    with pytest.raises(ValueError, match="replace"):
        blend_worlds.build_world(prepared(tmp_path), existing_environment="add")


# ---------------------------------------------------------------- build: World Material route


def material_record(name="Clouds"):
    return dict(
        name=name,
        key="blender-world-89abcdef0123456789abcdef",
        kind="material",
        reasons=[dict(rule="texture", message="The Environment Texture is ...: projection is Mirror Ball")],
        warnings=[],
        visibility=dict(camera=True, glossy=False),
        color_management=dict(colorspace="Linear Rec.709", color_interop_id="lin_rec709_scene"),
        material=dict(key="blender-world-89abcdef0123456789abcdef", name=name, nodes=[], outputs={}),
    )


def test_graph_world_becomes_a_world_material_set_on_every_rop(scene, tmp_path, monkeypatch):
    built = []

    def import_world_material(record, root, parent=None, diagnostics=None):
        built.append((record["name"], root))
        return parent.createNode("cycles::world_material::1", "Clouds_cdef").path()

    monkeypatch.setattr(blend_materials, "import_world_material", import_world_material)
    a, b = scene.rop("a"), scene.rop("b", world_source="color")
    result = blend_worlds.build_world(prepared(tmp_path, material_record()), mode="link")
    node = result["node"]
    assert (node.path(), result["route"]) == ("/mat/Clouds", "material")
    assert built == [("Clouds", str(tmp_path / "assets_blender/sky/tex"))]
    for rop in (a, b):
        assert (rop.parm("world_source").value, rop.parm("world_material").value) == ("material", "/mat/Clouds")
    assert "projection is Mirror Ball" in result["report"][0]
    assert any("glossy off in Blender" in line for line in result["report"])
    assert blend_library_materials.link_info(node)["route"] == "material"
    assert blend_worlds.build_world(prepared(tmp_path, material_record()), mode="link")["node"] is node


# ---------------------------------------------------------------- prepare: worker, packed HDRI, verdicts


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_prepare_places_a_packed_hdri_in_the_blend_tex_folder(scene, tmp_path, monkeypatch):
    from cycles_houdini.blender import blend_worker

    source = tmp_path / "library/sky.blend"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"BLENDER")
    data = b"#?RADIANCE packed"

    def export(path, names, staging, executable, **options):
        assert options["textures"] == "in_place" and names == ["Sky"]
        (Path(staging) / "textures").mkdir(parents=True)
        (Path(staging) / "textures/sky.hdr").write_bytes(data)
        image = dict(
            image_record("textures/sky.hdr"),
            filename="textures/sky.hdr",
            packed=True,
            dependencies=[dict(filename="textures/sky.hdr", sha256=_sha(data), packed=True, missing=False)],
        )
        return {"records": [simple_record(image=image, rotation=0.0)], "errors": {}}

    monkeypatch.setattr(blend_worker, "export_library_worlds", export)
    root = tmp_path / "hip/assets_blender"
    result = blend_worlds.prepare_world(source, "Sky", root=root, executable="blender")
    tex = root / "sky/tex"
    assert (tex / "sky.hdr").read_bytes() == data and result["root"] == str(tex)
    assert result["route"] == "light" and result["record"]["simple"]["image"]["filename"] == "sky.hdr"
    node = blend_worlds.build_world(result, mode="append")["node"]
    assert node.parm("env_map").value == (tex / "sky.hdr").as_posix()
    assert node.parmTuple("r")[1].value == -90.0
    # With the scene's unexpanded asset root, the path keeps its variable form.
    result["asset_root_text"] = "$HIP/assets_blender"
    node = blend_worlds.build_world(result, mode="append")["node"]
    assert node.parm("env_map").value == "$HIP/assets_blender/sky/tex/sky.hdr"


def test_prepare_verdicts_and_transport_failures(tmp_path, monkeypatch):
    from cycles_houdini.blender import blend_worker

    source = tmp_path / "sky.blend"
    source.write_bytes(b"BLENDER")
    unsupported = dict(material_record(), kind="unsupported", unsupported="World needs one active Cycles World Output")
    replies = iter(
        [
            {"records": [unsupported], "errors": {}},
            {"records": [], "errors": {"Sky": "World not found in sky.blend: Sky"}},
            RuntimeError("worker crashed"),
        ]
    )

    def export(*args, **kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        reply["records"] = [dict(r, name="Sky") for r in reply["records"]]
        return reply

    monkeypatch.setattr(blend_worker, "export_library_worlds", export)
    with pytest.raises(blend_worlds.BlendWorldError, match="Cycles World Output.*Mirror Ball") as verdict:
        blend_worlds.prepare_world(source, "Sky", root=tmp_path / "out", executable="blender")
    assert not isinstance(verdict.value, blend_library_materials.LibraryMaterialUnavailable)
    with pytest.raises(blend_worlds.BlendWorldError, match="not found"):
        blend_worlds.prepare_world(source, "Sky", root=tmp_path / "out", executable="blender")
    with pytest.raises(blend_library_materials.LibraryMaterialUnavailable, match="worker crashed"):
        blend_worlds.prepare_world(source, "Sky", root=tmp_path / "out", executable="blender")
    with pytest.raises(blend_library_materials.LibraryMaterialUnavailable, match="Blender not found"):
        blend_worlds.prepare_world(source, "Sky", root=tmp_path / "out", executable="")


def test_worker_command_passes_through(monkeypatch, tmp_path):
    from cycles_blender import blend_asset_scan, worker_server
    from cycles_houdini.blender import blend_worker

    source = tmp_path / "lib.blend"
    source.write_bytes(b"BLENDER")
    sent, received = [], []

    def disposable(command, *args, **kwargs):
        sent.append(command)
        return worker_server._asset_command(command)

    def export_named_worlds(path, names, output_dir, **options):
        received.append((names, options))
        return {"records": [simple_record("Sky")], "errors": {}}

    monkeypatch.setattr(blend_worker, "_disposable_request", disposable)
    monkeypatch.setattr(blend_asset_scan, "export_named_worlds", export_named_worlds)
    result = blend_worker.export_library_worlds(source, ["Sky", "Gone"], tmp_path / "out", "blender")
    assert sent[0]["command"] == "export_library_worlds" and sent[0]["textures"] == "in_place"
    assert received == [(["Sky", "Gone"], {"textures": "in_place"})]
    assert [r["name"] for r in result["records"]] == ["Sky"]
    assert result["errors"] == {"Gone": "Blender returned no world record for Gone"}
    monkeypatch.setattr(
        blend_asset_scan,
        "export_named_worlds",
        lambda *a, **k: {"records": [dict(kind="simple", name="Sky")], "errors": {}},
    )
    with pytest.raises(ValueError, match="Malformed Blender world record"):
        blend_worker.export_library_worlds(source, ["Sky"], tmp_path / "out", "blender")
    with pytest.raises(ValueError, match="texture mode"):
        blend_worker.export_library_worlds(source, ["Sky"], tmp_path / "out", "blender", textures="link")


# ---------------------------------------------------------------- reload a linked world


def test_reload_updates_a_linked_light_in_place(scene, tmp_path):
    node = blend_worlds.build_world(prepared(tmp_path), mode="link")["node"]
    node.parm("light_enable").set(0)
    record = simple_record(image=image_record("D:/Library/hdri/sky_8k.exr"), rotation=90.0)
    new, report = blend_worlds.reload_linked_world(node, prepared=prepared(tmp_path, record))
    assert new is node and node.name() == "Sky"
    values = node.values()
    assert values["env_map"] == "D:/Library/hdri/sky_8k.exr" and values["r"] == pytest.approx((0.0, -180.0, 0.0))
    assert values["light_enable"] == 0  # the artist's choice survives the reload
    assert node.parm("env_map").locked and not node.parm("light_enable").locked
    assert blend_library_materials.link_info(node)["kind"] == "world"
    with pytest.raises(blend_worlds.BlendWorldError, match="import it again"):
        blend_worlds.rebuild_linked_world(node, prepared(tmp_path, material_record("Sky")))


# ---------------------------------------------------------------- translator: world graphs


WORLD_INPUTS = {
    "world_material": [],
    "world_output": ["surface", "volume"],
    "background_shader": ["color", "strength"],
    "environment_texture": ["vector"],
}
WORLD_OUTPUTS = {
    "world_material": [],
    "world_output": [],
    "background_shader": ["background"],
    "environment_texture": ["color", "alpha"],
}
WORLD_DEFINITIONS = [
    dict(
        engine_type="background_shader",
        inputs=[axes.socket("color", "color"), axes.socket("strength")],
        outputs=[axes.socket("background", "closure")],
    ),
    dict(
        engine_type="environment_texture",
        inputs=[
            axes.socket("filename", "string"),
            axes.socket("colorspace", "string"),
            axes.socket("alpha_type", "enum", enum_values=axes.enum("unassociated", "associated", "ignore")),
            axes.socket("interpolation", "enum", enum_values=axes.enum("closest", "linear", "cubic", "smart")),
            axes.socket("projection", "enum", enum_values=axes.enum("equirectangular", "mirror_ball")),
            axes.socket("vector", "point", default_links=["position"]),
        ],
        outputs=[axes.socket("color", "color"), axes.socket("alpha")],
    ),
]


@pytest.fixture
def world_translator(world, monkeypatch):
    for kind, names in WORLD_INPUTS.items():
        monkeypatch.setitem(axes.INPUTS, kind, names)
    for kind, names in WORLD_OUTPUTS.items():
        monkeypatch.setitem(axes.OUTPUTS, kind, names)
    # Every axes node except Diffuse BSDF has a world adapter, as in the engine catalogue.
    definitions = [
        dict(d, world_graph_adapter=d["engine_type"] != "diffuse_bsdf") for d in axes.DEFINITIONS + WORLD_DEFINITIONS
    ]
    monkeypatch.setattr(blend_materials, "_find_space", lambda config, image: "Linear Rec.709 (sRGB)")
    import cycles_houdini

    monkeypatch.setattr(cycles_houdini, "shader_catalogue", lambda: dict(definitions=definitions))
    return world


def world_graph(vector=None):
    path = "D:/Library/hdri/sky.exr"
    texture = dict(
        id="e",
        name="Environment Texture",
        type="ShaderNodeTexEnvironment",
        properties=dict(interpolation="Linear", projection="MIRROR_BALL"),
        inputs=[axes.sock(0, "Vector", **(vector or dict(value=[0.0, 0.0, 0.0])))],
        outputs=[axes.sock(0, "Color", "RGBA")],
        image=image_record(path),
    )
    background = dict(
        id="b",
        name="Background",
        type="ShaderNodeBackground",
        properties={},
        inputs=[
            axes.sock(0, "Color", "RGBA", **axes.src("e", 0, "Color")),
            axes.sock(1, "Strength", "VALUE", value=2.0),
        ],
        outputs=[axes.sock(0, "Background", "SHADER")],
    )
    return dict(
        key="blender-world-0123456789abcdef01234567",
        name="Sky",
        nodes=[texture, background],
        outputs=dict(surface=axes.src("b", 0, "Background")),
        settings={},
        color_management=dict(colorspace="Linear Rec.709", color_interop_id="lin_rec709_scene"),
    )


def build_world_material(world, tmp_path, record):
    path = blend_materials.import_world_material(record, tmp_path, parent=world.mat)
    host = world.paths[path]
    return host, {node.name(): node for node in host.children()}


DIRECTIONS = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.3, -0.5, 0.8)]


def blender_axes(direction):
    return tuple(float(v) for v in blend_convert._CONVERT[:3, :3].T @ np.array(direction))


def test_world_graph_looks_up_the_texture_with_blender_axes(world_translator, tmp_path):
    host, nodes = build_world_material(world_translator, tmp_path, world_graph())
    assert host.type_name == "cycles::world_material::1" and "cycles_displacement_method" not in host.parms
    output = nodes["world_output"]
    assert output.wired["surface"] == (nodes["Background"], "background")
    assert nodes["Background"].wired["color"][0] is nodes["Environment_Texture"]
    texture = nodes["Environment_Texture"]
    assert texture.parm("filename").value == "D:/Library/hdri/sky.exr"
    assert texture.parm("projection").value == "mirror_ball"
    direction = nodes["world_direction"]
    assert direction.kind == "geometry"
    for d in DIRECTIONS:  # the unconnected Vector becomes the Blender-axes view direction
        leaves = {(direction.path(), "position"): d}
        assert axes.evaluate(*texture.wired["vector"], leaves) == pytest.approx(blender_axes(d))


def test_world_graph_texture_coordinate_is_converted_to_blender_axes(world_translator, tmp_path):
    coordinate = dict(
        id="t",
        name="Texture Coordinate",
        type="ShaderNodeTexCoord",
        properties=dict(from_instancer=False),
        inputs=[],
        outputs=[axes.sock(0, "Generated"), axes.sock(3, "Object")],
    )
    record = world_graph(vector=axes.src("t", 0, "Generated"))
    record["nodes"].insert(0, coordinate)
    host, nodes = build_world_material(world_translator, tmp_path, record)
    texture, generated = nodes["Environment_Texture"], nodes["Texture_Coordinate"]
    assert "world_direction" not in nodes
    for d in DIRECTIONS:
        leaves = {(generated.path(), "generated"): d}
        assert axes.evaluate(*texture.wired["vector"], leaves) == pytest.approx(blender_axes(d))


def test_world_graph_refuses_nodes_without_a_world_adapter(world_translator, tmp_path):
    record = world_graph()
    record["nodes"].append(axes.diffuse())
    record["outputs"] = dict(surface=axes.src("d", 0, "BSDF"))
    diagnostics = []
    with pytest.raises(blend_materials.BlendMaterialError, match="Unsupported connected Blender node type"):
        blend_materials.import_world_material(record, tmp_path, parent=world_translator.mat, diagnostics=diagnostics)
    assert world_translator.mat.children() == []
    assert diagnostics[-1]["code"] == "blender.material.unsupported"


def test_surface_materials_still_refuse_world_nodes(world, tmp_path):
    record = world_graph()
    record["outputs"] = dict(surface=axes.src("b", 0, "Background"))
    with pytest.raises(blend_materials.BlendMaterialError, match="ShaderNodeBackground|ShaderNodeTexEnvironment"):
        axes.build(world, tmp_path, record)
