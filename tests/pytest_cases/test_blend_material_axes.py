# SPDX-License-Identifier: Apache-2.0
"""Blender-world shader values get exact axis adapters at native world-space ports (fake `hou`)."""

import random
import sys
from types import SimpleNamespace

import pytest
from cycles_blender import blend_export
from cycles_houdini.blender import blend_materials

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

INPUTS = {
    "material": [],
    "surface_output": ["surface", "volume", "displacement"],
    "geometry": [],
    "texture_coordinate": [],
    "object_info": [],
    "vector_math": ["vector1", "vector2", "vector3", "scale"],
    "diffuse_bsdf": ["color", "roughness", "normal"],
    "vector_transform": ["vector"],
    "normal_map": ["strength", "color"],
    "displacement": ["height", "midlevel", "scale", "normal"],
    "vector_displacement": ["vector", "midlevel", "scale"],
    "combine_xyz": ["x", "y", "z"],
    "separate_xyz": ["vector"],
    "math": ["value1", "value2", "value3"],
    "value": [],
    "color": [],
}
OUTPUTS = {
    "material": [],
    "surface_output": [],
    "geometry": ["position", "normal", "tangent", "true_normal", "incoming", "backfacing"],
    "texture_coordinate": ["generated", "normal", "UV", "object", "camera", "window", "reflection"],
    "object_info": ["location", "color", "random"],
    "vector_math": ["vector", "value"],
    "diffuse_bsdf": ["bsdf"],
    "vector_transform": ["vector"],
    "normal_map": ["normal"],
    "displacement": ["displacement"],
    "vector_displacement": ["displacement"],
    "combine_xyz": ["vector"],
    "separate_xyz": ["x", "y", "z"],
    "math": ["value"],
    "value": ["value"],
    "color": ["color"],
}
TUPLES = {"vector": 3, "vector2": 3, "value": 1, "color": 3}


class Parm:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value

    def eval(self):
        return self.value

    def evalAsString(self):
        return str(self.value)


class ParmTuple(list):
    def set(self, value):
        self[:] = list(value)


class Node:
    def __init__(self, world, parent, type_name, name):
        self.world, self._parent, self.type_name, self._name = world, parent, type_name, name
        self.kind = type_name.split("::")[1] if "::" in type_name else type_name
        self.kids, self.user, self.parms, self.wired = [], {}, {}, {}
        self.position, self.destroyed = None, False
        world.paths[self.path()] = self

    def path(self):
        return (self._parent.path() if self._parent else "") + "/" + self._name

    def name(self):
        return self._name

    def createNode(self, type_name, name):
        base, index = name, 1
        while any(k._name == name for k in self.kids):
            name, index = base + str(index), index + 1
        child = Node(self.world, self, type_name, name)
        self.kids.append(child)
        return child

    def children(self):
        return list(self.kids)

    def destroy(self):
        self.destroyed = True
        if self._parent:
            self._parent.kids.remove(self)

    def setUserData(self, key, value):
        self.user[key] = value

    def parm(self, name):
        return self.parms.setdefault(name, Parm())

    def parmTuple(self, name):
        tuple_parm = self.parms.setdefault("tuple:" + name, ParmTuple([0.0] * TUPLES.get(name, 1)))
        return tuple_parm

    def inputNames(self):
        return INPUTS[self.kind]

    def outputNames(self):
        return OUTPUTS[self.kind]

    def setNamedInput(self, name, node, output):
        assert name in self.inputNames() and output in node.outputNames(), (self.kind, name, node.kind, output)
        self.wired[name] = (node, output)

    def inputs(self):
        return tuple(self.wired.get(name, (None,))[0] for name in self.inputNames())

    def size(self):
        return (1.0, 0.5)

    def setPosition(self, position):
        self.position = tuple(position)

    def layoutChildren(self):
        pass

    def createNetworkBox(self):
        raise AssertionError("no frames in these records")


def enum(*tokens):
    return [dict(token=t, engine_value=i) for i, t in enumerate(tokens)]


def socket(name, kind="float", **extra):
    return dict(id=name, label=name.replace("_", " ").title(), type=kind, **extra)


DEFINITIONS = [
    dict(engine_type="geometry", outputs=[socket(n, "point") for n in OUTPUTS["geometry"]], inputs=[]),
    dict(
        engine_type="texture_coordinate",
        inputs=[socket("from_dupli", "boolean")],
        outputs=[socket(n, "point") for n in OUTPUTS["texture_coordinate"]],
    ),
    dict(engine_type="object_info", inputs=[], outputs=[socket(n) for n in OUTPUTS["object_info"]]),
    dict(
        engine_type="vector_math",
        inputs=[socket(n, "vector") for n in ("vector1", "vector2", "vector3")]
        + [socket("scale"), socket("math_type", "enum", enum_values=enum("add", "dot_product"))],
        outputs=[socket("vector", "vector"), socket("value")],
    ),
    dict(
        engine_type="diffuse_bsdf",
        inputs=[socket("color", "color"), socket("roughness"), socket("normal", "normal", default_links=["normal"])],
        outputs=[socket("bsdf", "closure")],
    ),
    dict(
        engine_type="vector_transform",
        inputs=[
            socket("transform_type", "enum", enum_values=enum("vector", "point", "normal")),
            socket("convert_from", "enum", enum_values=enum("world", "object", "camera")),
            socket("convert_to", "enum", enum_values=enum("world", "object", "camera")),
            socket("vector", "vector"),
        ],
        outputs=[socket("vector", "vector")],
    ),
    dict(
        engine_type="normal_map",
        inputs=[
            socket("space", "enum", enum_values=enum("tangent", "object", "world", "blender_object", "blender_world")),
            socket("attribute", "string"),
            socket("strength"),
            socket("color", "color"),
        ],
        outputs=[socket("normal", "normal")],
    ),
    dict(
        engine_type="displacement",
        inputs=[
            socket("space", "enum", enum_values=enum("object", "world")),
            socket("height"),
            socket("midlevel"),
            socket("scale"),
            socket("normal", "normal", default_links=["normal"]),
        ],
        outputs=[socket("displacement", "vector")],
    ),
    dict(
        engine_type="vector_displacement",
        inputs=[
            socket("space", "enum", enum_values=enum("tangent", "object", "world")),
            socket("attribute", "string"),
            socket("vector", "color"),
            socket("midlevel"),
            socket("scale"),
        ],
        outputs=[socket("displacement", "vector")],
    ),
    dict(
        engine_type="combine_xyz",
        inputs=[socket("x"), socket("y"), socket("z")],
        outputs=[socket("vector", "vector")],
    ),
    dict(engine_type="separate_xyz", inputs=[socket("vector", "vector")], outputs=[socket(n) for n in "xyz"]),
    dict(
        engine_type="math",
        inputs=[socket("value1"), socket("value2"), socket("value3")],
        outputs=[socket("value")],
    ),
    dict(engine_type="value", inputs=[], outputs=[socket("value")]),
]
for definition in DEFINITIONS:
    definition["surface_graph_adapter"] = True


@pytest.fixture
def world(monkeypatch):
    world = SimpleNamespace(paths={})
    world.mat = Node(world, None, "matnet", "mat")
    hou = SimpleNamespace(
        node=lambda path: world.paths.get(path),
        Vector2=lambda x, y=None: (float(x), float(y)) if y is not None else tuple(map(float, x)),
        text=SimpleNamespace(expandString=lambda value: value, encodeAttrib=lambda value: value),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    identity = [[float(r == c) for c in range(3)] for r in range(3)]
    monkeypatch.setattr(blend_materials, "_color_matrices", lambda metadata: (None, identity, identity))
    import cycles_houdini
    from cycles_houdini import materials

    monkeypatch.setattr(cycles_houdini, "shader_catalogue", lambda: dict(definitions=DEFINITIONS))
    monkeypatch.setattr(materials, "material_graph", lambda host: None)
    return world


# ---------------------------------------------------------------- records


def sock(index, name, kind="VECTOR", **extra):
    return dict(index=index, identifier=name, name=name, type=kind, enabled=True, **extra)


def src(node, index, name):
    return dict(source=dict(node=node, socket=name, name=name, index=index))


def geometry(node_id="g"):
    names = ("Position", "Normal", "Tangent", "True Normal", "Incoming", "Backfacing")
    return dict(
        id=node_id,
        name="Geometry",
        type="ShaderNodeNewGeometry",
        properties={},
        inputs=[],
        outputs=[sock(i, n) for i, n in enumerate(names)],
    )


def vector_math(node_id, source=None, value=(0.0, 0.0, 0.0)):
    first = sock(0, "Vector", **(source or dict(value=list(value))))
    return dict(
        id=node_id,
        name="Vector Math " + node_id,
        type="ShaderNodeVectorMath",
        properties=dict(operation="ADD"),
        inputs=[first, sock(1, "Vector_001", value=[0.0, 0.0, 0.0]), sock(2, "Vector_002", value=[0.0, 0.0, 0.0])],
        outputs=[sock(0, "Vector"), sock(1, "Value", "VALUE")],
    )


def diffuse(node_id="d", normal=None):
    return dict(
        id=node_id,
        name="Diffuse",
        type="ShaderNodeBsdfDiffuse",
        properties={},
        inputs=[
            sock(0, "Color", "RGBA", value=[0.8, 0.8, 0.8, 1.0]),
            sock(1, "Roughness", "VALUE", value=0.0),
            sock(2, "Normal", **(normal or dict(value=[0.0, 0.0, 0.0]))),
        ],
        outputs=[sock(0, "BSDF", "SHADER")],
    )


def material(nodes, surface="d", displacement=None):
    outputs = dict(surface=src(surface, 0, "BSDF"))
    if displacement:
        outputs["displacement"] = src(*displacement)
    return dict(
        key="blender-material-0123456789abcdef01234567",
        name="Axes",
        nodes=nodes,
        outputs=outputs,
        settings=dict(displacement_method="BUMP"),
        color_management=dict(colorspace="Linear Rec.709", color_interop_id="lin_rec709_scene"),
    )


def build(world, tmp_path, record, diagnostics=None):
    paths = blend_materials.import_materials([record], tmp_path, parent=world.mat, diagnostics=diagnostics)
    host = world.paths[paths[record["key"]]]
    return host, {node.name(): node for node in host.children()}


# ---------------------------------------------------------------- evaluation of the fake graph


def evaluate(node, output, leaves):
    """Evaluate adapter nodes exactly as Cycles SVM does (float arithmetic, no fused ops)."""
    if (node.path(), output) in leaves:
        return leaves[node.path(), output]
    if node.kind == "separate_xyz":
        return evaluate(*node.wired["vector"], leaves)["xyz".index(output)]
    if node.kind == "combine_xyz":
        return tuple(evaluate(*node.wired[c], leaves) for c in "xyz")
    if node.kind == "math":

        def operand(name):
            return evaluate(*node.wired[name], leaves) if name in node.wired else node.parm(name).value

        a, b = operand("value1"), operand("value2")
        return {"multiply": lambda: a * b, "subtract": lambda: a - b}[node.parm("math_type").value]()
    raise AssertionError("unexpected node in an adapter: " + node.kind)


def source_of(node, name):
    return node.wired[name]


def helpers(nodes):
    return {name for name in nodes if name.startswith(("blender_axes", "houdini_axes"))}


H_TO_B = lambda v: (v[0], -v[2], v[1])  # noqa: E731 - Blender (x, y, z) from Houdini (x, z, -y)
B_TO_H = lambda v: (v[0], v[2], -v[1])  # noqa: E731


# ---------------------------------------------------------------- tests


def test_adapter_matrices_are_the_geometry_export_rotation_and_its_inverse():
    convert = blend_export._CONVERT
    assert [list(row[:3]) for row in convert[:3]] == [list(row) for row in blend_materials._BLENDER_TO_HOUDINI]
    # A pure rotation: no translation, no projective row, determinant +1, orthonormal.
    assert [row[3] for row in convert[:3]] == [0.0, 0.0, 0.0] and list(convert[3]) == [0.0, 0.0, 0.0, 1.0]
    forward, inverse = blend_materials._BLENDER_TO_HOUDINI, blend_materials._HOUDINI_TO_BLENDER
    product = [[sum(inverse[r][k] * forward[k][c] for k in range(3)) for c in range(3)] for r in range(3)]
    assert product == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    vector = (0.25, -1.5, 3.0)
    assert blend_materials._axes_value(list(vector), forward) == list(B_TO_H(vector))
    assert blend_materials._axes_value(list(vector), inverse) == list(H_TO_B(vector))
    # Encoded colours (2c - 1) negate as 1 - c; alpha is kept.
    assert blend_materials._axes_value([0.1, 0.2, 0.9, 1.0], forward, encoded=True) == [0.1, 0.9, 0.8, 1.0]


@pytest.mark.parametrize(
    "producer, index, name",
    [
        ("geometry", 0, "Position"),
        ("geometry", 1, "Normal"),
        ("geometry", 2, "Tangent"),
        ("geometry", 3, "True Normal"),
        ("geometry", 4, "Incoming"),
        ("texcoord", 6, "Reflection"),
        ("object_info", 0, "Location"),
    ],
)
def test_world_outputs_reach_graph_math_in_blender_axes(world, tmp_path, producer, index, name):
    if producer == "geometry":
        node = geometry("p")
    elif producer == "texcoord":
        names = ("Generated", "Normal", "UV", "Object", "Camera", "Window", "Reflection")
        node = dict(
            id="p",
            name="Texture Coordinate",
            type="ShaderNodeTexCoord",
            properties=dict(from_instancer=False),
            inputs=[],
            outputs=[sock(i, n) for i, n in enumerate(names)],
        )
    else:
        node = dict(
            id="p",
            name="Object Info",
            type="ShaderNodeObjectInfo",
            properties={},
            inputs=[],
            outputs=[sock(0, "Location"), sock(1, "Color", "RGBA"), sock(2, "Random", "VALUE")],
        )
    consumer = vector_math("m", src("p", index, name))
    record = material([node, consumer, diffuse(normal=src("m", 0, "Vector"))])
    host, nodes = build(world, tmp_path, record)
    adapter, output = source_of(nodes["Vector_Math_m"], "vector1")
    assert adapter.name() == "blender_axes" and output == "vector"
    split = adapter.wired["x"][0]
    native, socket_name = split.wired["vector"]
    assert native is nodes[node["name"].replace(" ", "_")] and socket_name == name.lower().replace(" ", "_")
    rng = random.Random(index)
    for _ in range(8):
        houdini = tuple(rng.uniform(-10, 10) for _ in range(3))
        assert evaluate(adapter, "vector", {(native.path(), socket_name): houdini}) == H_TO_B(houdini)
    # The Blender-world sum then meets a native world input: converted back exactly.
    back, _ = source_of(nodes["Diffuse"], "normal")
    assert back.name() == "houdini_axes"
    blender = (0.5, -2.0, 7.25)
    assert evaluate(back, "vector", {(nodes["Vector_Math_m"].path(), "vector"): blender}) == B_TO_H(blender)
    # Exactly one separate/negate/combine per direction.
    assert helpers(nodes) == {
        "blender_axes",
        "blender_axes_split",
        "blender_axes_y",
        "houdini_axes",
        "houdini_axes_split",
        "houdini_axes_z",
    }


def test_native_world_output_into_native_world_input_needs_no_adapter(world, tmp_path):
    record = material([geometry(), diffuse(normal=src("g", 1, "Normal"))])
    _, nodes = build(world, tmp_path, record)
    assert source_of(nodes["Diffuse"], "normal") == (nodes["Geometry"], "normal")
    assert not helpers(nodes)


def test_unconnected_world_normal_keeps_the_native_shading_default(world, tmp_path):
    _, nodes = build(world, tmp_path, material([diffuse()]))
    assert "normal" not in nodes["Diffuse"].wired and not helpers(nodes)


def vector_transform(node_id, convert_from, convert_to, source):
    return dict(
        id=node_id,
        name="Vector Transform " + node_id,
        type="ShaderNodeVectorTransform",
        properties=dict(vector_type="POINT", convert_from=convert_from, convert_to=convert_to),
        inputs=[sock(0, "Vector", **source)],
        outputs=[sock(0, "Vector")],
    )


def test_vector_transform_world_input_and_output(world, tmp_path):
    nodes_in = [
        vector_math("c"),  # a Blender-world constant vector
        vector_transform("to_object", "WORLD", "OBJECT", src("c", 0, "Vector")),
        vector_transform("to_world", "OBJECT", "WORLD", src("to_object", 0, "Vector")),
        vector_math("m", src("to_world", 0, "Vector")),
        diffuse(normal=src("m", 0, "Vector")),
    ]
    _, nodes = build(world, tmp_path, material(nodes_in))
    to_object, to_world = nodes["Vector_Transform_to_object"], nodes["Vector_Transform_to_world"]
    assert to_object.parm("convert_from").value == "world" and to_object.parm("convert_to").value == "object"
    # World input: Blender -> Houdini. Object -> object: untouched.
    adapter, _ = source_of(to_object, "vector")
    assert adapter.name() == "houdini_axes"
    v = (1.0, 2.0, 3.0)
    assert evaluate(adapter, "vector", {(nodes["Vector_Math_c"].path(), "vector"): v}) == B_TO_H(v)
    assert source_of(to_world, "vector") == (to_object, "vector")
    # World output: Houdini -> Blender before graph math.
    adapter, _ = source_of(nodes["Vector_Math_m"], "vector1")
    assert adapter.name() == "blender_axes"
    assert evaluate(adapter, "vector", {(to_world.path(), "vector"): v}) == H_TO_B(v)


def test_vector_transform_world_to_world_from_a_native_world_output_stays_native(world, tmp_path):
    record = material(
        [
            geometry(),
            vector_transform("t", "WORLD", "WORLD", src("g", 0, "Position")),
            diffuse(normal=src("t", 0, "Vector")),
        ]
    )
    _, nodes = build(world, tmp_path, record)
    assert source_of(nodes["Vector_Transform_t"], "vector") == (nodes["Geometry"], "position")
    assert source_of(nodes["Diffuse"], "normal") == (nodes["Vector_Transform_t"], "vector")
    assert not helpers(nodes)


def test_vector_transform_world_input_constant_is_rotated(world, tmp_path):
    item = vector_transform("t", "WORLD", "OBJECT", dict(value=[1.0, 2.0, 3.0]))
    _, nodes = build(world, tmp_path, material([item, vector_math("m", src("t", 0, "Vector")), diffuse()]))
    assert list(nodes["Vector_Transform_t"].parmTuple("vector")) == list(B_TO_H((1.0, 2.0, 3.0)))


@pytest.mark.parametrize("space", ["WORLD", "BLENDER_WORLD"])
def test_world_normal_map_color_is_permuted_in_the_encoded_domain(world, tmp_path, space):
    normal_map = dict(
        id="n",
        name="Normal Map",
        type="ShaderNodeNormalMap",
        properties=dict(space=space, uv_map=""),
        inputs=[sock(0, "Strength", "VALUE", value=1.0), sock(1, "Color", "RGBA", **src("c", 0, "Vector"))],
        outputs=[sock(0, "Normal")],
    )
    record = material([vector_math("c"), normal_map, diffuse(normal=src("n", 0, "Normal"))])
    _, nodes = build(world, tmp_path, record)
    host = nodes["Normal_Map"]
    assert host.parm("space").value == space.lower()
    adapter, _ = source_of(host, "color")
    assert adapter.name() == "houdini_axes_encoded"
    # The normal map's world normal feeds the BSDF directly (both Houdini world).
    assert source_of(nodes["Diffuse"], "normal") == (host, "normal")

    def decode(color):  # Cycles svm_node_normal_map, before normalization
        d = [2.0 * (c - 0.5) for c in color]
        return (d[0], -d[1], -d[2]) if space == "BLENDER_WORLD" else tuple(d)

    for color in ((0.5, 0.5, 1.0), (0.25, 0.75, 0.6), (1.0, 0.0, 0.5)):
        converted = evaluate(adapter, "vector", {(nodes["Vector_Math_c"].path(), "vector"): color})
        blender_normal = decode(color)
        assert decode(converted) == pytest.approx(B_TO_H(blender_normal), abs=1e-12)


def test_tangent_space_normal_map_is_untouched(world, tmp_path):
    normal_map = dict(
        id="n",
        name="Normal Map",
        type="ShaderNodeNormalMap",
        properties=dict(space="TANGENT", uv_map=""),
        inputs=[sock(0, "Strength", "VALUE", value=1.0), sock(1, "Color", "RGBA", **src("c", 0, "Vector"))],
        outputs=[sock(0, "Normal")],
    )
    _, nodes = build(world, tmp_path, material([vector_math("c"), normal_map, diffuse(normal=src("n", 0, "Normal"))]))
    assert source_of(nodes["Normal_Map"], "color") == (nodes["Vector_Math_c"], "vector") and not helpers(nodes)


@pytest.mark.parametrize("space", ["OBJECT", "WORLD"])
def test_displacement_output_is_a_native_world_vector_in_either_space(world, tmp_path, space):
    displacement = dict(
        id="x",
        name="Displacement",
        type="ShaderNodeDisplacement",
        properties=dict(space=space),
        inputs=[sock(0, "Height", "VALUE", value=0.5), sock(1, "Midlevel", "VALUE", value=0.0)],
        outputs=[sock(0, "Displacement")],
    )
    record = material([displacement, diffuse()], displacement=("x", 0, "Displacement"))
    host, nodes = build(world, tmp_path, record)
    assert nodes["Displacement"].parm("space").value == space.lower()
    assert source_of(nodes["surface_output"], "displacement") == (nodes["Displacement"], "displacement")
    assert not helpers(nodes)


def test_vector_displacement_world_input_and_graph_vector_into_material_displacement(world, tmp_path):
    vector_displacement = dict(
        id="x",
        name="Vector Displacement",
        type="ShaderNodeVectorDisplacement",
        properties=dict(space="WORLD"),
        inputs=[sock(0, "Vector", "RGBA", **src("c", 0, "Vector")), sock(1, "Midlevel", "VALUE", value=0.0)],
        outputs=[sock(0, "Displacement")],
    )
    record = material([vector_math("c"), vector_displacement, diffuse()], displacement=("x", 0, "Displacement"))
    _, nodes = build(world, tmp_path, record)
    adapter, _ = source_of(nodes["Vector_Displacement"], "vector")
    assert adapter.name() == "houdini_axes"
    assert source_of(nodes["surface_output"], "displacement") == (nodes["Vector_Displacement"], "displacement")
    # A Blender-world graph vector straight into the Material Output Displacement.
    world.paths.clear()
    world.mat = Node(world, None, "matnet", "mat")
    record = material([vector_math("c"), diffuse()], displacement=("c", 0, "Vector"))
    _, nodes = build(world, tmp_path, record)
    adapter, _ = source_of(nodes["surface_output"], "displacement")
    assert adapter.name() == "houdini_axes"
    v = (0.1, 0.2, 0.3)
    assert evaluate(adapter, "vector", {(nodes["Vector_Math_c"].path(), "vector"): v}) == B_TO_H(v)


def test_object_info_random_translates_with_a_diagnostic(world, tmp_path):
    info = dict(
        id="o",
        name="Object Info",
        type="ShaderNodeObjectInfo",
        properties={},
        inputs=[],
        outputs=[sock(0, "Location"), sock(1, "Color", "RGBA"), sock(2, "Random", "VALUE")],
    )
    math = dict(
        id="r",
        name="Darken",
        type="ShaderNodeVectorMath",
        properties=dict(operation="ADD"),
        inputs=[sock(0, "Vector", **src("o", 2, "Random")), sock(1, "Vector_001", value=[0.0, 0.0, 0.0])],
        outputs=[sock(0, "Vector"), sock(1, "Value", "VALUE")],
    )
    diagnostics = []
    host, nodes = build(world, tmp_path, material([info, math, diffuse()]), diagnostics)
    assert source_of(nodes["Darken"], "vector1") == (nodes["Object_Info"], "random")  # scalar: no adapter
    assert [d["code"] for d in diagnostics] == ["blender.material.approximation"]
    assert diagnostics[0]["severity"] == "warning"
    assert "Axes / Object Info: Object Info Random differs from Blender" in diagnostics[0]["message"]
    assert "Object Info Random differs from Blender" in host.user["cycles_blender_material_warnings"]


def test_exporter_placeholder_for_an_unsupported_material_fails_strict_import(world, tmp_path):
    record = dict(material([diffuse()]), unsupported="Image Texture has no image: Leaf", nodes=[], outputs={})
    diagnostics = []
    with pytest.raises(blend_materials.BlendMaterialError, match="Axes: Image Texture has no image"):
        blend_materials.import_materials([record], tmp_path, parent=world.mat, diagnostics=diagnostics)
    assert diagnostics[0]["code"] == "blender.material.unsupported" and not world.mat.children()


def test_layout_places_an_adapter_between_its_producer_and_consumer(world, tmp_path):
    g, m, d = geometry(), vector_math("m", src("g", 0, "Position")), diffuse(normal=src("m", 0, "Vector"))
    g.update(layout=[-600.0, 100.0], width=140.0)
    m.update(layout=[-300.0, 100.0], width=140.0)
    d.update(layout=[0.0, 100.0], width=140.0)
    record = material([g, m, d])
    record["output_layout"] = dict(name="Material Output", layout=[300.0, 100.0], width=140.0)
    _, nodes = build(world, tmp_path, record)
    assert all(node.position is not None for node in nodes.values())
    # Helpers stand in columns just left of the node they feed; the producer
    # moves left to make room, so the adapter sits between the two.
    consumer, producer = nodes["Vector_Math_m"].position[0], nodes["Geometry"].position[0]
    for name in ("blender_axes", "blender_axes_split", "blender_axes_y"):
        assert producer + 1.0 <= nodes[name].position[0] < consumer
    for name in ("houdini_axes", "houdini_axes_split", "houdini_axes_z"):
        assert consumer + 1.0 <= nodes[name].position[0] < nodes["Diffuse"].position[0]
    # Nothing was left unreachable beside the output.
    assert all(nodes[name].position[0] < nodes["surface_output"].position[0] for name in helpers(nodes))
