# SPDX-License-Identifier: Apache-2.0
"""Imported Blender materials keep the Blender node-editor layout (fake bpy and `hou`)."""

import sys
from types import SimpleNamespace

import pytest
from cycles_blender import blend_material_export
from cycles_houdini.blender import blend_library_materials, blend_materials

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


# ---------------------------------------------------------------- fake bpy


class BSocket:
    def __init__(self, node, name, kind="VALUE", value=0.0):
        self.node, self.name, self.identifier, self.type = node, name, name, kind
        self.enabled, self.default_value, self.links = True, value, []


class BLink:
    def __init__(self, source, target):
        self.from_socket, self.to_socket, self.is_valid, self.is_muted = source, target, True, False


class BNode:
    _next = 1

    def __init__(self, idname, name, location, *, inputs=(), outputs=(), width=140.0, parent=None, absolute=None):
        BNode._next += 1
        self.pointer = BNode._next
        self.bl_idname, self.name, self.label, self.location = idname, name, "", tuple(location)
        self.width, self.parent, self.mute, self.node_tree, self.hide = width, parent, False, None, False
        self.inputs = [BSocket(self, n) for n in inputs]
        self.outputs = [BSocket(self, n) for n in outputs]
        if absolute is not None:  # Blender 4.4+
            self.location_absolute = absolute

    def as_pointer(self):
        return self.pointer


class BFrame(BNode):
    def __init__(self, name, location, label="", color=None, parent=None, size=(300.0, 200.0)):
        super().__init__("NodeFrame", name, location, parent=parent, width=size[0])
        self.label, self.height = label, size[1]
        self.use_custom_color, self.color = color is not None, color or (0.608, 0.608, 0.608)


def link(source, target):
    target.links.append(BLink(source, target))


def group_node(name, location, tree, *, sockets=("V",), width=140.0, parent=None):
    node = BNode("ShaderNodeGroup", name, location, inputs=sockets, outputs=sockets, width=width, parent=parent)
    node.node_tree = tree
    return node


def group_tree(build):
    """A shader group tree: build(entry, exit) wires its internals."""
    entry = BNode("NodeGroupInput", "Group Input", (-1000.0, 0.0), outputs=("V",))
    exit = BNode("NodeGroupOutput", "Group Output", (500.0, 0.0), inputs=("V",))
    exit.is_active_output = True
    build(entry, exit)
    return SimpleNamespace(nodes=[entry, exit])


def export(output, socket="Surface"):
    graph = blend_material_export._Graph(images=None)
    terminal = graph.input(next(s for s in output.inputs if s.name == socket), ())
    return graph, terminal, graph.layout(output)


def by_name(graph):
    return {item["name"]: item for item in graph.nodes}


def height(node):
    return blend_material_export._height(node)


def test_absolute_positions_resolve_nested_frames_and_prefer_location_absolute():
    outer = BFrame("Frame", (-900.0, 500.0), label="Inputs", color=(0.2, 0.4, 0.6))
    inner = BFrame("Frame.001", (40.0, -30.0), parent=outer)
    noise = BNode("ShaderNodeTexNoise", "Noise", (20.0, -60.0), outputs=("Fac",), parent=inner)
    value = BNode("ShaderNodeValue", "Value", (5.0, 5.0), outputs=("Value",), absolute=(-500.0, 100.0))
    bsdf = BNode("ShaderNodeBsdfDiffuse", "Diffuse", (0.0, 300.0), inputs=("Color", "Roughness"), outputs=("BSDF",))
    output = BNode("ShaderNodeOutputMaterial", "Material Output", (300.0, 300.0), inputs=("Surface",))
    link(noise.outputs[0], bsdf.inputs[0])
    link(value.outputs[0], bsdf.inputs[1])
    link(bsdf.outputs[0], output.inputs[0])
    graph, _, record = export(output)
    nodes = by_name(graph)
    # Older Blender: location is relative to the parent frame, frames nest.
    assert nodes["Noise"]["layout"] == [-900.0 + 40.0 + 20.0, 500.0 - 30.0 - 60.0]
    assert nodes["Noise"]["location"] == [20.0, -60.0]  # raw key is kept
    assert nodes["Value"]["layout"] == [-500.0, 100.0]  # Blender 4.4+ location_absolute wins
    assert nodes["Diffuse"]["layout"] == [0.0, 300.0] and nodes["Diffuse"]["width"] == 140.0
    assert record["output_layout"] == dict(name="Material Output", layout=[300.0, 300.0], width=140.0)
    frames = {f["name"]: f for f in record["frames"]}
    assert set(frames) == {"Frame", "Frame.001"}  # only frames holding exported nodes
    noise_id = nodes["Noise"]["id"]
    assert frames["Frame.001"]["nodes"] == [noise_id] and frames["Frame.001"]["parent"] == frames["Frame"]["id"]
    assert frames["Frame"] == dict(
        id=frames["Frame"]["id"],
        name="Frame",
        label="Inputs",
        color=[0.2, 0.4, 0.6],
        parent=None,
        nodes=[noise_id],
        rect=[-900.0, 300.0, -600.0, 500.0],
    )
    assert frames["Frame.001"]["label"] == "Frame.001" and frames["Frame.001"]["color"] is None


def test_frame_without_exported_nodes_is_dropped_and_frameless_record_has_no_frames():
    BFrame("Empty", (0.0, 0.0))
    bsdf = BNode("ShaderNodeBsdfDiffuse", "Diffuse", (0.0, 0.0), outputs=("BSDF",))
    output = BNode("ShaderNodeOutputMaterial", "Material Output", (300.0, 0.0), inputs=("Surface",))
    link(bsdf.outputs[0], output.inputs[0])
    _, _, record = export(output)
    assert "frames" not in record and record["output_layout"]["layout"] == [300.0, 0.0]


def test_group_internals_replace_the_group_node_and_upstream_columns_make_room():
    def inner_build(entry, exit):
        multiply = BNode("ShaderNodeMath", "Multiply", (-100.0, 0.0), inputs=("A",), outputs=("V",))
        link(entry.outputs[0], multiply.inputs[0])
        link(multiply.outputs[0], exit.inputs[0])

    inner = group_tree(inner_build)
    holder = {}

    def outer_build(entry, exit):
        frame = BFrame("Group frame", (-650.0, 150.0), label="Inside")
        add = BNode("ShaderNodeMath", "Add", (50.0, -50.0), inputs=("A",), outputs=("V",), parent=frame)
        nested = group_node("Nested", (-200.0, 0.0), inner)
        link(entry.outputs[0], add.inputs[0])
        link(add.outputs[0], nested.inputs[0])
        link(nested.outputs[0], exit.inputs[0])
        holder.update(add=add, nested=nested)

    outer = group_tree(outer_build)
    top_frame = BFrame("Top", (-400.0, 200.0), label="Groups")
    noise = BNode("ShaderNodeTexNoise", "Noise", (-800.0, 400.0), outputs=("Fac",), width=160.0)
    group = group_node("Group", (100.0, -200.0), outer, parent=top_frame)  # absolute (-300, 0)
    bsdf = BNode("ShaderNodeBsdfDiffuse", "Diffuse", (0.0, 300.0), inputs=("Roughness", "Color"), outputs=("BSDF",))
    # Stacked under the group node, slightly left: shares its column, so it stays.
    below = BNode("ShaderNodeValue", "Below", (-320.0, -300.0), outputs=("Value",))
    link(below.outputs[0], bsdf.inputs[1])
    output = BNode("ShaderNodeOutputMaterial", "Material Output", (400.0, 300.0), inputs=("Surface",))
    link(noise.outputs[0], group.inputs[0])
    link(group.outputs[0], bsdf.inputs[0])
    link(bsdf.outputs[0], output.inputs[0])
    graph, _, record = export(output)
    nodes = by_name(graph)
    assert set(nodes) == {"Noise", "Group/Add", "Group/Nested/Multiply", "Diffuse", "Below"}
    add, multiply = holder["add"], nodes["Group/Nested/Multiply"]

    # Estimated heights: 40 + 22 per socket; every fake here has one input, one output.
    m_height = a_height = nested_height = group_height = 40.0 + 22.0 * 2
    assert height(add) == height(holder["nested"]) == height(group) == m_height
    # Innermost group: one 140-wide node replaces the 140-wide group node; right
    # edges align, so the block needs no room. Outer group space: Nested at
    # x=-200 -> Multiply at -200, centred vertically on Nested.
    multiply_y_outer = 0.0 - nested_height / 2 - (-m_height / 2)
    # Outer block: Add [-600, -460] (frame absolute -650 + 50) and Multiply [-200, -60].
    block = (-600.0, min(100.0 - a_height, multiply_y_outer - m_height), -60.0, max(100.0, multiply_y_outer))
    # Top level: Group (absolute -300, width 140) -> block right edge at -160.
    dx = -300.0 + 140.0 - block[2]
    dy = 0.0 - group_height / 2 - (block[1] + block[3]) / 2
    assert nodes["Group/Add"]["layout"] == [-600.0 + dx, 100.0 + dy]
    assert multiply["layout"] == [-200.0 + dx, multiply_y_outer + dy]
    assert nodes["Group/Add"]["width"] == add.width == 140.0
    # Room: the block is 540 wide, the group node 140, so Noise (left of the group) moves 400 left.
    assert nodes["Noise"]["layout"] == [-1200.0, 400.0]
    assert nodes["Diffuse"]["layout"] == [0.0, 300.0]  # right of the group: unchanged
    assert nodes["Below"]["layout"] == [-320.0, -300.0]  # overlaps the group node's column: unchanged
    assert record["output_layout"]["layout"] == [400.0, 300.0]

    frames = {f["label"]: f for f in record["frames"]}
    # A frame inside the group nests in the frame that holds the group node.
    assert frames["Inside"]["nodes"] == [nodes["Group/Add"]["id"]]
    assert frames["Inside"]["parent"] == frames["Groups"]["id"]
    assert frames["Groups"]["nodes"] == sorted([nodes["Group/Add"]["id"], multiply["id"]], key=lambda i: int(i[1:]))
    # The Groups frame rect: its left edge (-400) is left of the group node's x, so it stretches 400.
    assert frames["Groups"]["rect"] == [-800.0, 0.0, -100.0, 200.0]
    left, bottom, right, top = frames["Inside"]["rect"]
    assert (left, top) == (-650.0 + dx, 150.0 + dy) and right - left == 300.0 and top - bottom == 200.0


def test_default_records_without_layout_pass_omit_layout_keys():
    bsdf = BNode("ShaderNodeBsdfDiffuse", "Diffuse", (0.0, 0.0), outputs=("BSDF",))
    graph = blend_material_export._Graph(images=None)
    graph.output(bsdf.outputs[0], ())
    assert "layout" not in graph.nodes[0] and graph.nodes[0]["location"] == [0.0, 0.0]


# ---------------------------------------------------------------- fake hou

INPUTS = {
    "diffuse_bsdf": ["color", "roughness", "normal"],
    "value": [],
    "surface_output": ["surface", "volume", "displacement"],
    "vector_math": ["vector1", "vector2", "vector3", "scale"],
    "combine_xyz": ["x", "y", "z"],
    "material": [],
}
OUTPUTS = {
    "diffuse_bsdf": ["bsdf"],
    "value": ["value"],
    "surface_output": [],
    "vector_math": ["vector", "value"],
    "combine_xyz": ["vector"],
    "material": [],
}
TUPLES = {"vector2": 3}


class Parm:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value

    def evalAsString(self):
        return str(self.value)

    def unexpandedString(self):
        return str(self.value or "")


class ParmTuple(list):
    def set(self, value):
        self[:] = list(value)


class Vector2(tuple):
    def __new__(cls, x, y=None):
        return super().__new__(cls, (float(x), float(y)) if y is not None else tuple(float(v) for v in x))


class Box:
    def __init__(self, world):
        self.items, self.comment, self.color, self.fitted = [], "", None, False
        world.boxes.append(self)

    def setComment(self, text):
        self.comment = text

    def setColor(self, color):
        self.color = color

    def addItem(self, item):
        assert not self.fitted
        self.items.append(item)

    def fitAroundContents(self):
        self.fitted = True


class Node:
    def __init__(self, world, parent, type_name, name):
        self.world, self._parent, self.type_name, self._name = world, parent, type_name, name
        self.kind = type_name.split("::")[1] if "::" in type_name else type_name
        self.kids, self.user, self.parms, self.wired = [], {}, {}, {}
        self.position, self.laid_out, self.destroyed = None, False, False
        world.paths[self.path()] = self

    def path(self):
        return (self._parent.path() if self._parent else "") + "/" + self._name

    def name(self):
        return self._name

    def setName(self, name, unique_name=False):
        del self.world.paths[self.path()]
        base, index = name, 1
        while unique_name and any(k._name == name for k in self._parent.kids if k is not self):
            name, index = base + str(index), index + 1
        self._name = name
        self.world.paths[self.path()] = self

    def type(self):
        return SimpleNamespace(name=lambda: self.type_name)

    def createNode(self, type_name, name):
        base, index = name, 1
        while any(k._name == name for k in self.kids):
            name, index = base + str(index), index + 1
        child = Node(self.world, self, type_name, name)
        self.kids.append(child)
        return child

    def children(self):
        return list(self.kids)

    def allSubChildren(self):
        return [n for k in self.kids for n in (k, *k.allSubChildren())]

    def destroy(self):
        self.destroyed = True
        if self._parent:
            self._parent.kids.remove(self)

    def setUserData(self, key, value):
        self.user[key] = value

    def userData(self, key):
        return self.user.get(key)

    def destroyUserData(self, key):
        self.user.pop(key, None)

    def parm(self, name):
        return self.parms.setdefault(name, Parm())

    def parmTuple(self, name):
        return self.parms.setdefault("tuple:" + name, ParmTuple([0.0] * TUPLES.get(name, 1)))

    def inputNames(self):
        return INPUTS[self.kind]

    def outputNames(self):
        return OUTPUTS[self.kind]

    def setNamedInput(self, name, node, output):
        assert name in self.inputNames() and output in node.outputNames(), (self.kind, name, output)
        self.wired[name] = node

    def inputs(self):
        return tuple(self.wired.get(name) for name in self.inputNames())

    def size(self):
        return Vector2(1.0, 0.5)

    def setPosition(self, position):
        self.position = tuple(position)

    def layoutChildren(self):
        self.laid_out = True

    def createNetworkBox(self):
        return Box(self.world)


@pytest.fixture
def world(monkeypatch):
    world = SimpleNamespace(paths={}, boxes=[])
    world.mat = Node(world, None, "matnet", "mat")
    hou = SimpleNamespace(
        node=lambda path: world.paths.get(path),
        Vector2=Vector2,
        Color=lambda rgb: ("color", tuple(rgb)),
        text=SimpleNamespace(expandString=lambda value: value, encodeAttrib=lambda value: value),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    # Blender Rec.709 working space vs a different Houdini scene_linear: closure
    # colour inputs get a builder-owned conversion (combine_xyz + 3 dot products).
    matrix = [[0.6, 0.3, 0.1], [0.1, 0.8, 0.1], [0.0, 0.1, 0.9]]
    identity = [[float(r == c) for c in range(3)] for r in range(3)]
    monkeypatch.setattr(blend_materials, "_color_matrices", lambda metadata: (None, matrix, identity))
    import cycles_houdini
    from cycles_houdini import materials

    def socket(name, kind="float"):
        return dict(id=name, label=name.title(), type=kind)

    definitions = [
        dict(
            engine_type="diffuse_bsdf",
            surface_graph_adapter=True,
            inputs=[socket("color", "color"), socket("roughness"), socket("normal", "vector")],
            outputs=[socket("bsdf", "closure")],
        ),
        dict(engine_type="value", surface_graph_adapter=True, inputs=[], outputs=[socket("value")]),
    ]
    monkeypatch.setattr(cycles_houdini, "shader_catalogue", lambda: dict(definitions=definitions))
    monkeypatch.setattr(materials, "material_graph", lambda host: None)
    return world


def blender_record(layout=True, frames=None):
    def sock(index, name, kind, **extra):
        return dict(index=index, identifier=name, name=name, type=kind, enabled=True, **extra)

    value = dict(
        id="n0",
        name="Value",
        type="ShaderNodeValue",
        properties={},
        inputs=[],
        outputs=[sock(0, "Value", "VALUE", value=0.5)],
        location=[0.0, 0.0],
    )
    diffuse = dict(
        id="n1",
        name="Diffuse",
        type="ShaderNodeBsdfDiffuse",
        properties={},
        inputs=[
            sock(0, "Color", "RGBA", source=dict(node="n0", socket="Value", name="Value", index=0)),
            sock(1, "Roughness", "VALUE", value=0.25, value_type="VALUE", explicit_default=True),
        ],
        outputs=[sock(0, "BSDF", "SHADER")],
        location=[0.0, 0.0],
    )
    record = dict(
        key="blender-material-0123456789abcdef01234567",
        name="Layout",
        nodes=[value, diffuse],
        outputs=dict(surface=dict(source=dict(node="n1", socket="BSDF", name="BSDF", index=0))),
        settings=dict(displacement_method="BUMP"),
        color_management=dict(colorspace="Linear Rec.709", color_interop_id="lin_rec709_scene"),
    )
    if layout:
        value.update(layout=[-400.0, 100.0], width=140.0)
        diffuse.update(layout=[-100.0, 120.0], width=140.0)
        record["output_layout"] = dict(name="Material Output", layout=[200.0, 120.0], width=140.0)
    if frames:
        record["frames"] = frames
    return record


def build(world, record, tmp_path):
    paths = blend_materials.import_materials([record], tmp_path, parent=world.mat)
    return world.paths[paths[record["key"]]]


def named(host):
    return {node.name(): node for node in host.children()}


def test_builder_places_nodes_from_blender_layout_and_helpers_left_of_their_consumer(world, tmp_path):
    host = build(world, blender_record(), tmp_path)
    nodes = named(host)
    assert not host.laid_out and not world.boxes
    # Scale 0.01 (fake VOPs are 1 unit wide, Blender nodes 140): node tops are the
    # scaled Blender tops; positions are tile lower-left corners (tile height 0.5).
    diffuse, output, value = nodes["Diffuse"], nodes["surface_output"], nodes["Value"]
    assert diffuse.position == pytest.approx((-1.0, 1.2 - 0.3))
    assert output.position == pytest.approx((2.0, 1.2 - 0.3))
    # Helpers feeding Diffuse: column 1 = colour conversion combine + roughness
    # group default, column 2 = the three dot products. Each column is 1.0 + 0.5
    # wide, so Value (left of Diffuse in Blender) moves 3.0 further left.
    assert value.position == pytest.approx((-4.0 - 3.0, 1.0 - 0.3))
    combine, default = nodes["color_to_working"], nodes["group_default"]
    assert combine.position == pytest.approx((-2.5, 1.2 - 0.3))
    # combine lists 3 connectors (0.4 + 3 * 0.2 = 1.0 tall), then a 0.25 gap.
    assert default.position == pytest.approx((-2.5, 1.2 - 1.0 - 0.25 - 0.3))
    tops = [nodes["color_to_working_" + c].position for c in "xyz"]
    # dot products list 4 connectors: 1.2 tall each.
    assert tops == [pytest.approx((-4.0, t - 0.3)) for t in (1.2, 1.2 - 1.45, 1.2 - 2.9)]
    # Value's right edge (-6) stays clear of the furthest helper column (-4).
    assert value.position[0] + 1.0 < tops[0][0]


def test_builder_drops_vertically_overlapping_nodes_in_a_column(world, tmp_path):
    record = blender_record()
    record["nodes"][1]["inputs"][0] = dict(
        index=0, identifier="Color", name="Color", type="RGBA", enabled=True, value=[0.5, 0.5, 0.5, 1.0]
    )
    record["nodes"][1]["inputs"][1].pop("explicit_default")
    # Value directly above Diffuse in the same column, closer than Diffuse is tall.
    record["nodes"][0]["layout"] = [-100.0, 180.0]
    record["outputs"]["surface"] = dict(source=dict(node="n1", socket="BSDF", name="BSDF", index=0))
    host = build(world, record, tmp_path)
    nodes = named(host)
    assert nodes["Value"].position == pytest.approx((-1.0, 1.8 - 0.3))
    # Value is 0.6 tall (one connector row), so Diffuse drops to 1.8 - 0.6 - 0.25.
    assert nodes["Diffuse"].position == pytest.approx((-1.0, 1.8 - 0.6 - 0.25 - 0.3))


def test_builder_without_layout_keeps_automatic_layout(world, tmp_path):
    record = blender_record(
        layout=False, frames=[dict(id="f0", label="Ignored", color=None, parent=None, nodes=["n0"])]
    )
    host = build(world, record, tmp_path)
    assert host.laid_out and not world.boxes
    assert all(node.position is None for node in host.children())
    partial = blender_record()
    del partial["output_layout"]
    assert not blend_materials.has_layout(partial)
    partial = blender_record()
    del partial["nodes"][0]["layout"]
    assert not blend_materials.has_layout(partial)


def test_frames_become_nested_network_boxes(world, tmp_path):
    frames = [
        dict(id="f0", name="Frame", label="Shading", color=[0.2, 0.4, 0.6], parent=None, nodes=["n0", "n1"]),
        dict(id="f1", name="Frame.001", label="Inputs", color=None, parent="f0", nodes=["n0"]),
        dict(id="f2", name="Frame.002", label="Out", color=None, parent=None, nodes=["output"]),
    ]
    host = build(world, blender_record(frames=frames), tmp_path)
    nodes = named(host)
    boxes = {box.comment: box for box in world.boxes}
    assert set(boxes) == {"Shading", "Inputs", "Out"} and all(box.fitted for box in boxes.values())
    assert boxes["Inputs"].items == [nodes["Value"]] and boxes["Inputs"].color is None
    helpers = {nodes[n] for n in ("color_to_working", "group_default", *("color_to_working_" + c for c in "xyz"))}
    shading = boxes["Shading"].items
    assert shading[0] is nodes["Diffuse"] and set(shading[1:-1]) == helpers and shading[-1] is boxes["Inputs"]
    assert boxes["Shading"].color == ("color", (0.2, 0.4, 0.6))
    assert boxes["Out"].items == [nodes["surface_output"]]


def test_library_build_and_linked_reload_rebuild_the_same_layout(world, tmp_path):
    record = blender_record(frames=[dict(id="f0", label="Out", color=None, parent=None, nodes=["output"])])
    prepared = dict(record=record, root=str(tmp_path), source=str(tmp_path / "lib.blend"), name="Layout")

    def positions(material):
        return {node.name(): node.position for node in material.children()}

    first = blend_library_materials.build_library_material(prepared, parent=world.mat)
    first_positions = positions(first)
    # Linked reload builds the re-exported record again through import_materials.
    second = blend_library_materials.build_library_material(prepared, parent=world.mat)
    assert positions(second) == first_positions
    assert first_positions["surface_output"] == pytest.approx((2.0, 1.2 - 0.3))
    assert len(world.boxes) == 2
