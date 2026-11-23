# SPDX-License-Identifier: Apache-2.0
"""Blender -> Houdini converter (blend_convert) against synthetic exporter results and a fake `hou`.

Covers attribute mapping (UV/Cd selection, EDGE promotion, reserved names, vertex groups),
winding and axes, materials (mat_* groups, Material SOP, linked reuse), packed/hierarchy
structures with instance sharing, the files layout (assets_blender/<blend>/geo/<asset>, one
cycles_import.json per blend folder), frame sequences, embedded and reference storage, cancellation,
undo grouping and the import_blend wrapper. More layout cases: test_blend_convert_layout.py.
"""

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

C = np.array(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)), dtype=float)


# ---------------------------------------------------------------- fake hou geometry


class FakePoint:
    def __init__(self, geometry, index):
        self.geometry, self.index = geometry, index


class FakePrim:
    def __init__(self, geometry, kind, points, closed=True):
        self.geometry, self.kind, self.points, self.closed = geometry, kind, list(points), closed
        self.embedded = self.transform = None

    def setEmbeddedGeometry(self, geometry):
        self.embedded = geometry

    def setTransform(self, matrix):
        self.transform = matrix
        self.geometry.positions[self.points[0]] = tuple(matrix[3][:3])


class FakeGroup:
    def __init__(self):
        self.members = []

    def add(self, items):
        self.members.extend(items)


class FakeGeometry:
    def __init__(self):
        self.positions, self.prims, self.attribs = [], [], {}
        self.groups, self.edge_groups, self.frozen = {}, {}, False

    def createPoints(self, positions):
        start = len(self.positions)
        self.positions.extend(tuple(p) for p in positions)
        return tuple(FakePoint(self, start + i) for i in range(len(positions)))

    def createPolygons(self, points, is_closed=True):
        created = tuple(FakePrim(self, "poly", [int(i) for i in pts], is_closed) for pts in points)
        self.prims.extend(created)
        return created

    def createPacked(self, typename, point=None):
        assert typename == "PackedGeometry" and point is not None
        prim = FakePrim(self, "packed", [point.index])
        self.prims.append(prim)
        return prim

    def counts(self):
        return {
            "point": len(self.positions),
            "vertex": sum(len(prim.points) for prim in self.prims),
            "prim": len(self.prims),
        }

    def addAttrib(self, kind, name, default, transform_as_normal=False, create_local_variable=False):
        assert (kind, name) not in self.attribs
        self.attribs[(kind, name)] = dict(default=default, normal=transform_as_normal, values=None)

    def _set(self, kind, name, values, size_kind):
        attrib = self.attribs[(kind, name)]
        default = attrib["default"]
        width = len(default) if isinstance(default, tuple) else 1
        assert len(values) == self.counts()[kind] * width, (name, len(values), self.counts()[kind], width)
        attrib["values"] = list(values)
        attrib["size_kind"] = size_kind

    def __getattr__(self, name):
        # set{Point,Vertex,Prim}{Float,Int,String}AttribValues
        for owner in ("Point", "Vertex", "Prim"):
            for size_kind in ("Float", "Int", "String"):
                if name == f"set{owner}{size_kind}AttribValues":
                    return lambda attrib, values: self._set(owner.lower(), attrib, values, size_kind)
        raise AttributeError(name)

    def createPrimGroup(self, name):
        return self.groups.setdefault(name, FakeGroup())

    def createEdgeGroup(self, name):
        return self.edge_groups.setdefault(name, FakeGroup())

    def findEdge(self, a, b):
        if getattr(self, "_edges", None) is None or self._edges[0] != len(self.prims):
            edges = set()
            for prim in self.prims:
                ring = prim.points + ([prim.points[0]] if prim.closed else [])
                edges.update(tuple(sorted((ring[i], ring[i + 1]))) for i in range(len(ring) - 1))
            self._edges = (len(self.prims), edges)
        pair = tuple(sorted((a.index, b.index)))
        return pair if pair in self._edges[1] else None

    def freeze(self, read_only=False):
        self.frozen = read_only
        return self

    def values(self, owner, name):
        return self.attribs[(owner, name)]["values"]

    def content(self):
        index = {id(prim): number for number, prim in enumerate(self.prims)}
        return dict(
            positions=self.positions,
            prims=[(p.kind, p.points, p.closed, id(p.embedded) if p.embedded else None) for p in self.prims],
            attribs={f"{k}:{n}": v["values"] for (k, n), v in sorted(self.attribs.items())},
            groups={n: sorted(index[id(p)] for p in g.members) for n, g in self.groups.items()},
        )

    def saveToFile(self, path):
        # bgeo carries a creation date: identical geometry never has identical bytes.
        Path(path).write_text(json.dumps(dict(date=os.urandom(4).hex(), content=self.content()), default=str))


# ---------------------------------------------------------------- fake hou nodes


class Parm:
    def __init__(self, node, name):
        self.node, self.name, self.value = node, name, None

    def set(self, value):
        self.node.host.log.append(("set", self.node.path(), self.name, self.node.host.depth))
        self.value = value

    def eval(self):
        return self.value

    def unexpandedString(self):
        return self.value

    def pressButton(self):
        self.node.host.log.append(("press", self.node.path(), self.name, self.node.host.depth))

    def path(self):
        return self.node.path() + "/" + self.name

    def isLocked(self):
        return False

    def lock(self, on):
        pass


class Node:
    def __init__(self, host, parent, type_name, name):
        self.host, self._parent, self.type_name, self._name = host, parent, type_name, name
        self._parms, self.children_, self.user = {}, [], {}
        self.flags, self.inputs, self.transform, self.pos, self.comment = set(), [], None, None, ""

    def path(self):
        return (self._parent.path().rstrip("/") if self._parent else "") + "/" + self._name

    def name(self):
        return self._name

    def parent(self):
        return self._parent

    def type(self):
        return SimpleNamespace(name=lambda: self.type_name, category=lambda: "cat")

    def createNode(self, type_name, name, run_init_scripts=True):
        self.host.log.append(("create", type_name, name, self.host.depth))
        base, index = name, 1
        while any(child._name == name for child in self.children_):
            index += 1
            name = base + str(index)
        node = Node(self.host, self, type_name, name)
        self.children_.append(node)
        return node

    def children(self):
        return list(self.children_)

    def allSubChildren(self):
        return [d for c in self.children_ for d in [c, *c.allSubChildren()]]

    def node(self, name):
        return next((c for c in self.children_ if c._name == name), None)

    def parm(self, name):
        return self._parms.setdefault(name, Parm(self, name))

    def parms(self):
        return list(self._parms.values())

    def evalParm(self, name):
        return self.parm(name).value

    def setFirstInput(self, node):
        self.inputs = [node]

    def setDisplayFlag(self, on):
        self.flags.add("display")

    def setRenderFlag(self, on):
        self.flags.add("render")

    def setParmTransform(self, matrix):
        self.transform = matrix

    def setUserData(self, key, value):
        self.user[key] = value

    def userData(self, key):
        return self.user.get(key)

    def destroyUserData(self, key):
        self.user.pop(key, None)

    def setPosition(self, position):
        self.pos = tuple(position)

    def moveToGoodPosition(self):
        pass

    def layoutChildren(self):
        pass

    def setCurrent(self, *args, **kwargs):
        pass

    def setName(self, name, unique_name=False):
        self._name = name

    def setComment(self, text):
        self.comment = text


@pytest.fixture
def fake(monkeypatch, tmp_path):
    hou = ModuleType("hou")
    hou.log, hou.depth = [], 0

    @contextlib.contextmanager
    def group(label):
        hou.log.append(("undo", label))
        hou.depth += 1
        try:
            yield
        finally:
            hou.depth -= 1

    hou.undos = SimpleNamespace(group=group)
    root = Node(hou, None, "root", "")
    obj = Node(hou, root, "obj", "obj")
    mat = Node(hou, root, "mat", "mat")
    root.children_ = [obj, mat]

    def find(path):
        def walk(node):
            if node.path() == path:
                return node
            for child in node.children_:
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(root) if path else None

    hou.node = find
    hou.Geometry = FakeGeometry
    hou.Matrix4 = lambda rows: tuple(tuple(row) for row in rows)
    hou.attribType = SimpleNamespace(Point="point", Vertex="vertex", Prim="prim", Global="detail")
    hip = tmp_path / "shots"
    hip.mkdir()
    (hip / "scene.hip").write_bytes(b"hip")
    hou.scene = SimpleNamespace(new=False, path=str(hip / "scene.hip"), hip=str(hip))
    hou.hipFile = SimpleNamespace(isNewFile=lambda: hou.scene.new, path=lambda: hou.scene.path)
    hou.text = SimpleNamespace(expandString=lambda value: value.replace("$HIP", hou.scene.hip))
    hou.getenv = lambda name, default=None: default
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_CACHE", str(tmp_path / "cache"))
    from cycles_houdini.blender import blend_assets_layout

    # pytest's temporary folders are long; the 240-character rule has its own test.
    monkeypatch.setattr(blend_assets_layout, "MAX_PATH", 4096)
    blend = tmp_path / "library" / "props" / "props.blend"
    blend.parent.mkdir(parents=True)
    blend.write_bytes(b"BLENDER")
    built = []

    from cycles_houdini.blender import blend_library_materials

    def build_material(prepared, *, parent=None, texture_root=None, diagnostics=None):
        node = (parent or mat).createNode("cycles_material", blend_library_materials.node_name(prepared["name"]))
        # As build_library_material: the source and Blender name identify an appended copy.
        node.setUserData("cycles_blender_library_source", str(prepared["source"]).replace("\\", "/"))
        node.setUserData("cycles_blender_library_material", prepared["name"])
        built.append((prepared["name"], prepared, hou.depth))
        for image in (node_["image"] for node_ in prepared["record"].get("nodes", ()) if "image" in node_):
            if image.get("missing") and diagnostics is not None:
                diagnostics.append(
                    dict(code="material.missing_texture", message="texture not found: " + image["filename"])
                )
        return node

    monkeypatch.setattr(blend_library_materials, "build_library_material", build_material)
    assets = hip / "assets_blender" / "props"
    return SimpleNamespace(hou=hou, obj=obj, mat=mat, hip=hip, blend=blend, built=built, tmp=tmp_path, assets=assets)


# ---------------------------------------------------------------- synthetic exporter results


def mesh_record(key="mesh_000000", name="QuadTri"):
    arrays = {}

    def add(field, value):
        arrays[key + "/" + field] = np.asarray(value)
        return key + "/" + field

    positions = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (3, 3, 3)]
    loops = [0, 1, 2, 3, 1, 4, 2]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0), (1, 4), (4, 2), (2, 5)]
    record = dict(
        key=key,
        name=name,
        vertex_count=6,
        polygon_count=2,
        corner_count=7,
        arrays=dict(
            positions=add("positions", np.array(positions, np.float32)),
            edges=add("edges", np.array(edges, np.int32)),
            polygon_counts=add("polygon_counts", np.array([4, 3], np.int32)),
            vertex_indices=add("vertex_indices", np.array(loops, np.int32)),
            material_indices=add("material_indices", np.array([0, 2], np.int32)),
            smooth=add("smooth", np.array([1, 0], np.uint8)),
            normals=add("normals", np.tile(np.array([0, 0, 1], np.float32), (7, 1))),
        ),
        uv_layers=[
            dict(name="UVMap", array=add("uv_0", np.arange(14, dtype=np.float32).reshape(7, 2)), active_render=False),
            dict(name="Render UV", array=add("uv_1", np.full((7, 2), 0.5, np.float32)), active_render=True),
        ],
        default_uv="Render UV",
        color_attributes=[
            dict(name="Col", domain="POINT", array=add("color_0", np.tile([0.2, 0.4, 0.6, 0.5], (6, 1)))),
            dict(name="Paint", domain="CORNER", array=add("color_1", np.ones((7, 4), np.float32))),
        ],
        default_color="Col",
        attributes=[
            dict(
                name="temperature", domain="POINT", data_type="FLOAT", array=add("a0", np.arange(6, dtype=np.float32))
            ),
            dict(name="crease", domain="EDGE", data_type="FLOAT", array=add("a1", np.arange(7, dtype=np.float32))),
            dict(
                name="sharp_edge",
                domain="EDGE",
                data_type="BOOLEAN",
                array=add("a2", np.array([1, 0, 0, 0, 0, 0, 0], np.uint8)),
            ),
            dict(name="N", domain="FACE", data_type="FLOAT", array=add("a3", np.array([7, 8], np.float32))),
            dict(
                name="velocity",
                domain="POINT",
                data_type="FLOAT_VECTOR",
                array=add("a4", np.tile([0, 0, 1], (6, 1)).astype(np.float32)),
            ),
            dict(name="id", domain="POINT", data_type="INT", array=add("a5", np.arange(10, 16, dtype=np.int32))),
            dict(name="material_index", domain="FACE", data_type="INT", array=add("a6", np.array([0, 2], np.int32))),
            dict(name="count", domain="FACE", data_type="INT", array=add("a7", np.array([3, 4], np.int32))),
            dict(name="corner w", domain="CORNER", data_type="FLOAT", array=add("a8", np.arange(7, dtype=np.float32))),
        ],
        skipped_attributes=[dict(name="rotation", domain="POINT", data_type="QUATERNION")],
        vertex_groups=[dict(name="Group A", array=add("vg0", np.array([1, 0, 0.5, 0, 0, 0], np.float32)))],
    )
    return record, arrays


MATERIALS = [
    dict(key="kRed", name="Red Paint", local_name="Red Paint", library="", nodes=[], outputs={}, default=False),
    dict(key="kBlue", name="Blue", local_name="Blue", library="", nodes=[], outputs={}, default=False),
    dict(key="kDefault", name="Blender default surface", default=True, nodes=[], outputs={}, library=""),
]


def instance(key, transform, name, owner, depth, mesh="mesh_000000", kind="mesh", keys=("kRed", "kDefault", "kBlue")):
    return dict(
        name=name,
        source_name=name,
        mesh_key=mesh,
        transform=[float(v) for v in np.asarray(transform).reshape(-1)],
        material_keys=list(keys),
        owner=owner,
        depth=depth,
        geometry_type=kind,
        is_instance=True,
    )


def translate(x, y, z):
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    return matrix


def scene_manifest(frame=1.0, offset=0.0):
    """Collection "Props": Parent (empty) > Child (QuadTri); Tree (QuadTri) generates two instances."""
    record, arrays = mesh_record()
    copy, copy_arrays = mesh_record("mesh_000001", "QuadTri.001")  # same content, other key
    arrays.update(copy_arrays)
    parent_world = C @ translate(0, 0, 2 + offset)
    child_world = C @ translate(1, 0, 2 + offset)
    tree_world = C @ translate(5, 0, 0)
    objects = [
        dict(
            name="Parent",
            type="EMPTY",
            parent=None,
            collection_path=["Props"],
            transform=list(parent_world.reshape(-1)),
        ),
        dict(
            name="Child",
            type="MESH",
            parent="Parent",
            collection_path=["Props", "Sub"],
            transform=list(child_world.reshape(-1)),
        ),
        dict(name="Tree", type="MESH", parent=None, collection_path=["Props"], transform=list(tree_world.reshape(-1))),
    ]
    instances = [
        instance("i0", child_world, "Child", "Child", 1),
        instance("i1", tree_world, "Tree", "Tree", 1),
        instance("i2", tree_world @ translate(0, 1, 0), "Tree", "Tree", 3, mesh="mesh_000000"),
        instance("i3", tree_world @ translate(0, 2, 0), "Tree", "Tree", 3, mesh="mesh_000001"),
    ]
    manifest = dict(
        selector_type="COLLECTION",
        selector="Props",
        frame=frame,
        scene="Scene",
        blender_version="5.3.0",
        meshes=[record, copy],
        curves=[],
        pointclouds=[],
        instances=instances,
        objects=objects,
        materials=MATERIALS,
        diagnostics=[],
        units=dict(system="METRIC", scale_length=1.0),
    )
    return manifest, arrays


def distinct_scene_manifest():
    """scene_manifest with distinct Child and Tree shapes; Tree's two generated instances stay identical."""
    manifest, arrays = scene_manifest()
    for key, scale in (("mesh_000002", 2.0), ("mesh_000003", 3.0)):
        record, extra = mesh_record(key, "Shape" + key[-1])
        extra[key + "/positions"] = np.asarray(extra[key + "/positions"], dtype=float) * scale
        arrays.update(extra)
        manifest["meshes"].append(record)
    manifest["instances"][0]["mesh_key"] = "mesh_000002"  # Child
    manifest["instances"][1]["mesh_key"] = "mesh_000003"  # Tree's own mesh
    return manifest, arrays


def fake_evaluate(manifest_for, calls=None):
    """Stands in for blend_worker.evaluate_many (positional signature)."""

    def evaluate(source, executable, requests, texture_directory, timeout, source_config=None, consume=None):
        if calls is not None:
            calls.append([dict(request) for request in requests])
        results = []
        for index, request in enumerate(requests):
            Path(texture_directory, "request-" + str(index)).mkdir(parents=True, exist_ok=True)
            results.append(consume(index, manifest_for(request)))
        return results

    return evaluate


def snapshot(fake, **options):
    from cycles_houdini.blender import blend_convert

    options.setdefault("executable", sys.executable)
    source = options.pop("source", fake.blend)
    return blend_convert.import_snapshot(str(source), options.pop("selection", [("collection", "Props")]), **options)


def run(fake, structure="packed", storage="files", manifest_for=None, calls=None, **options):
    from cycles_houdini.blender import blend_convert

    snap = snapshot(fake, structure=structure, storage=storage, **options)
    payload = blend_convert.prepare_import(
        snap, evaluate=fake_evaluate(manifest_for or (lambda request: scene_manifest()), calls)
    )
    messages = []
    nodes = blend_convert.build_import(payload, report=lambda text, error=False: messages.append((text, error)))
    return nodes, payload, messages


# ---------------------------------------------------------------- pure helpers


def test_winding_names_and_groups():
    from cycles_houdini.blender import blend_convert as bc

    assert bc.winding_permutation([4, 3]).tolist() == [0, 3, 2, 1, 4, 6, 5]
    used = set()
    # Clashes with reserved or already written names get a blend_ prefix (artist rule U5).
    assert bc.attribute_name("P", used) == "blend_P"
    assert bc.attribute_name("uv3", used) == "uv3"
    assert bc.attribute_name("my attr", used) == "my_attr"
    assert bc.attribute_name("my-attr", used) == "my_attr_2"
    assert bc.attribute_name("3d", used) == "_3d"
    assert bc.attribute_name("velocity", used, mapped="v") == "v"
    assert bc.attribute_name("v", used, mapped="v") == "blend_v"  # mapped name already taken
    assert bc.material_group("Red Paint.001") == "mat_Red_Paint_001"
    assert bc.safe_component("Fir Sapling (v2)", "x") == "Fir_Sapling_v2"
    assert bc.frame_list(None) == [None] and bc.frame_list((1, 5, 2)) == [1, 3, 5]
    with pytest.raises(bc.BlendImportError):
        bc.frame_list((1.5, 3, 1))
    with pytest.raises(bc.BlendImportError, match="storage"):
        bc.validate_options("embedded", "packed", "link", (1, 3, 1))


def test_mesh_attribute_mapping_axes_and_winding():
    from cycles_houdini.blender import blend_convert as bc

    record, arrays = mesh_record()
    report = bc.Report()
    geo = bc.mesh_geo(record, arrays, ["kRed", "kDefault", "kBlue"], {"kRed": "Red Paint", "kBlue": "Blue"}, report)
    # Axes: Blender (x, y, z) -> Houdini (x, z, -y); winding reversed keeping the first corner.
    assert geo.P[2].tolist() == [1, 0, -1] and geo.P[5].tolist() == [3, 3, -3]
    assert geo.poly_points.tolist() == [0, 3, 2, 1, 1, 2, 4]
    # The loose edge (2, 5) is an open 2-point polyline.
    assert geo.line_counts.tolist() == [2] and geo.line_points.tolist() == [2, 5]
    attribs = geo.attribs
    assert attribs["N"].owner == "vertex" and attribs["N"].normal
    assert np.allclose(attribs["N"].values[:7], [0, 1, 0]) and np.allclose(attribs["N"].values[7:], 0)
    # Active render UV map -> uv, the other keeps its Blender name (no uv2 numbering); records
    # without keep_name (older exports) also keep the active map's name.
    assert np.allclose(attribs["uv"].values[:7], [0.5, 0.5, 0])
    assert attribs["UVMap"].values[:7, :2].tolist() == np.arange(14).reshape(7, 2)[[0, 3, 2, 1, 4, 6, 5]].tolist()
    assert "Render_UV" in attribs and "uv2" not in attribs
    # Active render colour -> point Cd + Alpha (not all 1); another colour keeps its name (RGBA).
    assert attribs["Cd"].owner == "point" and np.allclose(attribs["Cd"].values, [0.2, 0.4, 0.6])
    assert np.allclose(attribs["Alpha"].values, 0.5) and attribs["Paint"].width == 4
    # Generic attributes by domain; reserved names prefixed unless they are the same meaning.
    assert attribs["temperature"].owner == "point"
    assert attribs["blend_N"].owner == "prim" and attribs["blend_N"].values[:2, 0].tolist() == [7, 8]
    assert attribs["v"].vector and np.allclose(attribs["v"].values, [0, 1, 0])
    assert attribs["id"].kind == "int" and attribs["id"].values[:, 0].tolist() == list(range(10, 16))
    assert attribs["count"].owner == "prim" and attribs["count"].values[:, 0].tolist() == [3, 4, 0]
    assert attribs["corner_w"].values[:7, 0].tolist() == [0, 3, 2, 1, 4, 6, 5]
    assert "material_index" not in attribs and "blender_material_index" not in attribs
    # EDGE: Houdini has no edge attributes, skipped and reported (artist rule U5); sharp_edge -> edge group.
    assert "crease" not in attribs and any(text.startswith("crease (EDGE FLOAT)") for text in report.skipped)
    assert geo.edge_groups["sharp_edges"].tolist() == [[0, 1]]
    assert any("rotation" in text for text in report.skipped)
    # Vertex group -> point float attribute.
    assert attribs["Group_A"].values[:, 0].tolist() == [1, 0, 0.5, 0, 0, 0]
    assert report.renamed["vertex group Group A"] == "Group_A"
    # Material slots -> prim groups and blender_material strings (the default slot has none).
    assert geo.prim_groups["mat_Red_Paint"].tolist() == [0] and geo.prim_groups["mat_Blue"].tolist() == [1]
    assert attribs["blender_material"].values == ["Red Paint", "Blue", ""]
    assert geo.material_groups == {"mat_Red_Paint": "kRed", "mat_Blue": "kBlue"}


def test_curves_and_point_clouds():
    from cycles_houdini.blender import blend_convert as bc

    arrays = {
        "c/positions": np.arange(21, dtype=np.float32).reshape(7, 3),
        "c/offsets": np.array([0, 3, 7], np.int32),
        "c/radius": np.full(7, 0.25, np.float32),
        "c/a0": np.array([1, 0], np.int32),
        "c/a1": np.array([[0.1, 0.2], [0.3, 0.4]], np.float32),
        "p/positions": np.zeros((2, 3), np.float32),
        "p/radius": np.array([0.1, 0.2], np.float32),
    }
    curves = dict(
        key="c",
        arrays=dict(positions="c/positions", offsets="c/offsets", radius="c/radius"),
        curve_types=[0],
        cyclic_count=1,
        attributes=[
            dict(name="material_index", domain="CURVE", data_type="INT", array="c/a0"),
            dict(name="surface_uv_coordinate", domain="CURVE", data_type="FLOAT2", array="c/a1"),
        ],
        skipped_attributes=[],
    )
    report = bc.Report()
    geo = bc.curves_geo(curves, arrays, ["kHair", "kTip"], {"kHair": "Hair", "kTip": "Tip"}, report)
    assert geo.line_counts.tolist() == [3, 4] and geo.poly_counts.tolist() == []
    assert np.allclose(geo.attribs["width"].values, 0.5)  # Houdini width is a diameter
    assert geo.attribs["surface_uv_coordinate"].owner == "prim"
    assert geo.prim_groups == {"mat_Tip": [0], "mat_Hair": [1]} or (
        geo.prim_groups["mat_Hair"].tolist() == [1] and geo.prim_groups["mat_Tip"].tolist() == [0]
    )
    assert any("Catmull-Rom" in text for text in report.lossy) and any("cyclic" in text for text in report.lossy)
    cloud = dict(key="p", arrays=dict(positions="p/positions", radius="p/radius"), attributes=[], skipped_attributes=[])
    points = bc.points_geo(cloud, arrays, [], {}, report)
    assert points.num_points == 2 and points.num_prims == 0
    assert points.attribs["pscale"].values[:, 0].tolist() == pytest.approx([0.1, 0.2])


def test_structures_share_instances_and_rebuild_parents():
    from cycles_houdini.blender import blend_convert as bc

    manifest, arrays = scene_manifest()
    names = {"kRed": "Red Paint", "kBlue": "Blue"}
    # packed: identical content under two exporter keys is one shared child geometry.
    frame = bc.assemble(manifest, arrays, "packed", names, bc.Report())
    geo = frame["geo"]
    assert len(frame["children"]) == 1 and len(set(geo.packed_children)) == 1 and geo.num_prims == 4
    assert geo.attribs["name"].values == ["Child", "Tree", "Tree", "Tree"]
    assert geo.attribs["path"].values == ["/Props/Sub/Child", "/Props/Tree", "/Props/Tree/Tree", "/Props/Tree/Tree"]
    # Packed point = Houdini translation of C * M * C^-1 (Blender (5, 1, 0) -> Houdini (5, 0, -1)).
    assert geo.P[2].tolist() == [5, 0, -1]
    # merged was removed (Load As: Unpacked on the Blender File node replaces it).
    with pytest.raises(bc.BlendImportError, match="Merged structure was removed"):
        bc.assemble(manifest, arrays, "merged", names, bc.Report())
    # hierarchy: Child under Parent with a local transform; Tree keeps its own mesh unpacked and
    # its generated instances packed; the empty becomes a null.
    objects = {o["name"]: o for o in bc.assemble(manifest, arrays, "hierarchy", names, bc.Report())["objects"]}
    assert objects["Parent"]["node_type"] == "null" and objects["Child"]["parent"] == "Parent"
    assert np.allclose(objects["Child"]["local"], translate(1, 0, 0))  # Houdini axes: Blender x stays x
    assert np.allclose(objects["Parent"]["local"][:3, 3], [0, 2, 0])  # Blender z=2 -> Houdini y=2
    tree = objects["Tree"]["geo"]
    assert len(tree.poly_counts) == 2 and len(tree.packed_children) == 2 and len(objects["Tree"]["children"]) == 1
    assert tree.P[6].tolist() == [0, 0, -1] and tree.P[7].tolist() == [0, 0, -2]  # local offsets of the instances
    assert np.allclose(objects["Child"]["geo"].P[2], [1, 0, -1])  # geometry stays in local space


def test_frame_spill_round_trip(tmp_path):
    from cycles_houdini.blender import blend_convert as bc

    manifest, arrays = scene_manifest()
    for structure in ("packed", "hierarchy"):
        frame = bc.assemble(manifest, arrays, structure, {"kRed": "Red Paint"}, bc.Report())
        path = bc._spill(frame, tmp_path / (structure + ".npz"))
        loaded = bc.load_frame(path)
        if structure == "packed":
            assert bc.geo_digest(loaded["geo"], loaded["children"]) == bc.geo_digest(frame["geo"], frame["children"])
        else:
            assert [o["name"] for o in loaded["objects"]] == [o["name"] for o in frame["objects"]]
            assert np.allclose(loaded["objects"][1]["local"], frame["objects"][1]["local"])


# ---------------------------------------------------------------- build


def file_text(path):
    return json.loads(Path(path).read_text())


def import_json(fake, blend="props"):
    return json.loads((fake.hip / "assets_blender" / blend / "cycles_import.json").read_text())


def test_packed_files_layout_manifest_materials_and_no_overwrite(fake):
    calls = []
    nodes, payload, messages = run(fake, "packed", calls=calls)
    [request] = calls[0]
    assert request["selector_type"] == "COLLECTION" and request["frame"] == "scene"
    assert request["vertex_groups"] and request["curves"] and request["pointclouds"] and request["object_tree"]
    # A final Subsurf stays unapplied (render-time subdivision) unless apply_subdivision is set.
    assert request["native_subdivision"] is True and "material_export" not in request
    # Textures stay where the .blend points; only packed images are staged (blend-import.md: Textures in place).
    assert request["material_textures"] == "in_place"
    # Everything that touches HOM happened inside one undo group.
    log = fake.hou.log
    assert [entry for entry in log if entry[0] == "undo"] == [("undo", "Import Blender assets")]
    assert all(entry[-1] == 1 for entry in log if entry[0] in ("create", "set"))
    assert all(depth == 1 for *_, depth in fake.built)
    [geo] = nodes
    assert geo.type_name == "geo" and geo.name() == "Props" and geo.parent() is fake.obj
    file_sop, out = geo.node("file1"), geo.node("OUT")
    assert out.inputs == [file_sop] and {"display", "render"} <= out.flags
    assert geo.node("material1") is None  # packed contents carry shop_materialpath instead
    # <asset root>/<blend stem>/geo/<asset>/<asset>.bgeo.sc (blend-import.md: Folder layout).
    folder = fake.assets / "geo" / "Props"
    assert file_sop.evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.bgeo.sc"
    content = file_text(folder / "Props.bgeo.sc")["content"]
    assert [kind for kind, *_ in content["prims"]] == ["packed"] * 4
    assert len({embedded for *_, embedded in content["prims"]}) == 1  # one shared, frozen child
    assert content["attribs"]["prim:path"][0] == "/Props/Sub/Child"
    # Materials: built once each (linked), used by the children through shop_materialpath.
    assert sorted(name for name, *_ in fake.built) == ["Blue", "Red Paint"]
    assert fake.built[0][1]["asset_root_text"] == "$HIP/assets_blender"
    red = fake.hou.node("/mat/Red_Paint")
    link = json.loads(red.userData("cycles_asset_link"))
    assert link["name"] == "Red Paint" and "texture_root" not in link
    # One cycles_import.json per blend folder replaces the per-asset manifests.
    data = import_json(fake)
    assert data["source"]["path"] == str(fake.blend).replace("\\", "/")
    assert data["names"]["assets"] == {"collection:Props": "Props"}
    record = data["assets"]["Props"]
    options = record["options"]
    assert options["structure"] == "packed" and options["blender"]["version"] == "5.3.0"
    assert [entry["path"] for entry in record["files"]] == ["Props.bgeo.sc"] and record["files"][0]["digest"]
    assert any("crease" in text for text in options["report"]["skipped"])
    assert [o["name"] for o in options["blender"]["objects"]] == ["Parent", "Child", "Tree"]
    assert options["structures"] == {"packed": ["Props.bgeo.sc"]}
    assert record["scenes"] == [str(fake.hip / "scene.hip").replace("\\", "/")]
    user = json.loads(geo.userData("cycles_blender_import"))
    assert user["manifest"] == (fake.assets / "cycles_import.json").as_posix()
    assert user["blend_folder"] == fake.assets.as_posix() and user["asset_folder"] == folder.as_posix()
    assert not Path(payload["snapshot"]["asset_root_abs"]).is_relative_to(fake.blend.parent)
    assert sorted(p.name for p in folder.iterdir()) == ["Props.bgeo.sc"]  # no <asset>.json any more

    # Same content again: the file is reused (not rewritten) and the linked materials too.
    before = (folder / "Props.bgeo.sc").read_text()
    nodes, _, messages = run(fake, "packed")
    assert nodes[0].node("file1").evalParm("file").endswith("/Props.bgeo.sc")
    assert (folder / "Props.bgeo.sc").read_text() == before
    assert len(fake.built) == 2 and any("Reused identical geometry" in text for text, _ in messages)

    # Different content (the .blend changed): the same files are replaced, File SOPs reloaded.
    nodes, _, messages = run(fake, "packed", manifest_for=lambda request: scene_manifest(offset=1.0))
    assert nodes[0].node("file1").evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.bgeo.sc"
    third = (folder / "Props.bgeo.sc").read_text()
    assert third != before
    assert sorted(p.name for p in folder.iterdir()) == ["Props.bgeo.sc"]
    assert any("Replaced the previous import" in text for text, _ in messages)

    # Another .blend file with the same stem gets its own blend folder and never touches these files.
    other = fake.tmp / "library" / "alt" / "props.blend"
    other.parent.mkdir()
    other.write_bytes(b"BLENDER")
    nodes, _, _ = run(fake, "packed", source=other)
    assert nodes[0].node("file1").evalParm("file") == "$HIP/assets_blender/props__alt/geo/Props/Props.bgeo.sc"
    assert sorted(p.name for p in folder.iterdir()) == ["Props.bgeo.sc"]
    assert (folder / "Props.bgeo.sc").read_text() == third
    assert import_json(fake, "props__alt")["source"]["path"] == str(other).replace("\\", "/")


def test_reimport_replaces_and_deletes_leftovers(fake):
    calls = []

    def manifest_for(request):
        return scene_manifest(frame=request["frame"], offset=request["frame"])

    first, _, _ = run(fake, "packed")
    file_sops = lambda: [n for n in fake.obj.allSubChildren() if n.type_name == "file"]  # noqa: E731
    fake.hou.sopNodeTypeCategory = lambda: SimpleNamespace(
        nodeTypes=lambda: {"file": SimpleNamespace(instances=file_sops)}
    )
    run(fake, "packed", frames=(1, 3, 1), manifest_for=manifest_for)
    folder = fake.assets / "geo" / "Props"
    assert len(list(folder.glob("Props.*.bgeo.sc"))) == 3
    # The earlier single-frame import now reads the replacement instead of a deleted file.
    assert first[0].node("file1").evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.$F4.bgeo.sc"
    assert not (folder / "Props.bgeo.sc").exists()
    # A shorter range replaces the sequence; frame 3 of the old import is deleted.
    _, _, messages = run(fake, "packed", frames=(1, 2, 1), manifest_for=manifest_for, calls=calls)
    assert sorted(p.name for p in folder.glob("*.bgeo.sc")) == ["Props.0001.bgeo.sc", "Props.0002.bgeo.sc"]
    assert any("1 old file(s) deleted" in text for text, _ in messages)
    # Other structures keep their own files next to it, in the same asset folder and record.
    run(fake, "hierarchy", materials="none")
    names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert names == [
        "Props.0001.bgeo.sc",
        "Props.0002.bgeo.sc",
        "hierarchy/Child.bgeo.sc",
        "hierarchy/Tree.bgeo.sc",
    ]
    structures = import_json(fake)["assets"]["Props"]["options"]["structures"]
    assert structures["hierarchy"][0] == "hierarchy/Child.bgeo.sc" and structures["packed"][0] == "Props.0001.bgeo.sc"
    # A packed re-import replaces only packed files: the hierarchy files of the asset stay.
    run(fake, "packed")
    names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert names == ["Props.bgeo.sc", "hierarchy/Child.bgeo.sc", "hierarchy/Tree.bgeo.sc"]
    files = [entry["path"] for entry in import_json(fake)["assets"]["Props"]["files"]]
    assert files == names


def test_recorded_names_never_leave_the_asset_folder(fake):
    run(fake, "packed")
    path = fake.assets / "cycles_import.json"
    data = json.loads(path.read_text())
    outside = fake.tmp / "outside.bgeo.sc"
    outside.write_text("keep")
    # A tampered JSON listing a file outside the asset folder: nothing at all is deleted.
    data["assets"]["Props"]["files"].append({"path": "../../../../outside.bgeo.sc", "digest": "x"})
    data["assets"]["Props"]["files"].append({"path": "gone.bgeo.sc", "digest": "x"})
    (fake.assets / "geo" / "Props" / "gone.bgeo.sc").write_text("old")
    path.write_text(json.dumps(data))
    _, _, messages = run(fake, "packed")
    assert outside.read_text() == "keep" and (fake.assets / "geo" / "Props" / "gone.bgeo.sc").exists()
    assert any("were not deleted" in text and error for text, error in messages)


def test_hierarchy_material_sop_and_append_materials(fake):
    run(fake, "hierarchy", materials="append", manifest_for=lambda request: distinct_scene_manifest())
    tree = fake.obj.node("Tree")
    material = tree.node("material1")
    assert material.inputs == [tree.node("file1")] and tree.node("OUT").inputs == [material]
    assert material.evalParm("num_materials") == 2
    entries = {material.evalParm(f"group{i}"): material.evalParm(f"shop_materialpath{i}") for i in (1, 2)}
    assert entries == {"mat_Blue": "/mat/Blue", "mat_Red_Paint": "/mat/Red_Paint"}
    assert material.userData("cycles_blender_material") == "1" and tree.node("file1").userData("cycles_blender_source")
    assert fake.hou.node("/mat/Red_Paint").userData("cycles_asset_link") is None  # append: editable copies
    folder = fake.assets / "geo" / "Props"
    content = file_text(folder / "hierarchy" / "Tree.bgeo.sc")["content"]
    # Unpacked polygons are assigned by group; the packed instances carry shop_materialpath inside.
    assert len(content["groups"]["mat_Red_Paint"]) == 1 and [p[0] for p in content["prims"]].count("packed") == 2


def test_append_reimport_reuses_the_editable_material_copies(fake):
    run(fake, "hierarchy", materials="append", manifest_for=lambda request: distinct_scene_manifest())
    first = fake.obj.node("Tree").node("material1")
    assert len(fake.built) == 2
    run(fake, "hierarchy", materials="append", manifest_for=lambda request: distinct_scene_manifest())
    assert len(fake.built) == 2  # no new copies: the scene's appended materials are used again
    second = fake.obj.node("Tree_1").node("material1")
    assert [first.evalParm(f"shop_materialpath{i}") for i in (1, 2)] == [
        second.evalParm(f"shop_materialpath{i}") for i in (1, 2)
    ]


def test_hierarchy_objects_parents_and_files(fake):
    nodes, _, _ = run(fake, "hierarchy", materials="none")
    [root] = nodes
    assert root.type_name == "null" and root.name() == "Props"
    children = {node.name(): node for node in fake.obj.children()}
    assert children["Parent"].type_name == "null" and children["Parent"].inputs == [root]
    assert children["Child"].inputs == [children["Parent"]] and children["Tree"].inputs == [root]
    assert children["Child"].transform[3][:3] == (1.0, 0.0, 0.0)
    assert children["Parent"].transform[3][:3] == (0.0, 2.0, 0.0)
    assert children["Child"].node("material1") is None and not fake.built  # materials="none"
    folder = fake.assets / "geo" / "Props"
    assert children["Tree"].node("file1").evalParm("file").endswith("/Props/hierarchy/Tree.bgeo.sc")
    assert children["Child"].node("file1").evalParm("file").endswith("/Props/hierarchy/Child.bgeo.sc")
    assert sorted(p.name for p in (folder / "hierarchy").glob("*.bgeo.sc")) == ["Child.bgeo.sc", "Tree.bgeo.sc"]
    tree = file_text(folder / "hierarchy" / "Tree.bgeo.sc")["content"]
    assert [kind for kind, *_ in tree["prims"]].count("packed") == 2
    record = import_json(fake)["assets"]["Props"]
    assert [entry["path"] for entry in record["files"]] == ["hierarchy/Child.bgeo.sc", "hierarchy/Tree.bgeo.sc"]
    assert record["options"]["structures"] == {"hierarchy": ["hierarchy/Child.bgeo.sc", "hierarchy/Tree.bgeo.sc"]}
    names = import_json(fake)["names"]["objects"]["Props"]
    assert names == {"Child": "Child", "Tree": "Tree"}
    assert json.loads(root.userData("cycles_blender_import"))["asset_folder"] == folder.as_posix()


def test_object_selection_hierarchy_has_no_collection_null(fake):
    def manifest_for(request):
        manifest, arrays = scene_manifest()
        manifest.update(
            selector_type="OBJECT",
            selector="Tree",
            objects=[o for o in manifest["objects"] if o["name"] == "Tree"],
            instances=[dict(i, depth=0 if i["depth"] == 1 else 2) for i in manifest["instances"][1:]],
        )
        return manifest, arrays

    nodes, _, _ = run(fake, "hierarchy", selection=[("object", "Tree")], manifest_for=manifest_for)
    assert [node.name() for node in nodes] == ["Tree"] and nodes[0].type_name == "geo"


def test_frame_sequence_files(fake):
    calls = []

    def manifest_for(request):
        return scene_manifest(frame=request["frame"], offset=request["frame"])

    nodes, payload, messages = run(fake, "packed", frames=(1, 3, 1), manifest_for=manifest_for, calls=calls)
    frames = [request["frame"] for request in calls[0]]
    assert frames == [1.0, 2.0, 3.0]
    assert ["material_export" in request for request in calls[0]] == [False, True, True]
    assert all(isinstance(frame, str) for item in payload["items"] for frame in item["frames"])  # spilled
    assert nodes[0].node("file1").evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.$F4.bgeo.sc"
    folder = fake.assets / "geo" / "Props"
    assert sorted(p.name for p in folder.glob("*.bgeo.sc")) == [f"Props.000{i}.bgeo.sc" for i in (1, 2, 3)]
    assert any("Frame sequences" in text for text, _ in messages)


def test_embedded_and_reference_storage(fake, monkeypatch):
    nodes, _, _ = run(fake, "packed", storage="embedded")
    stash = nodes[0].node("stash1")
    assert isinstance(stash.evalParm("stash"), FakeGeometry)
    assert not (fake.hip / "assets_blender").exists()  # no geometry files and no packed images
    from cycles_houdini.blender import blend_catalogue, blend_convert

    calls = []
    monkeypatch.setattr(blend_catalogue, "initialize_rows", lambda node: calls.append("init"))
    monkeypatch.setattr(blend_catalogue, "bind_targets", lambda node: calls.append("bind"))
    monkeypatch.setattr(blend_catalogue, "refresh_node", lambda node: calls.append("refresh"))

    def no_blender(*args, **kwargs):
        raise AssertionError("reference storage must not run Blender")

    snap = snapshot(
        fake, storage="reference", include_disabled=True, selection=[("collection", "Props"), ("object", "Lamp 1")]
    )
    payload = blend_convert.prepare_import(snap, evaluate=no_blender)
    nodes = blend_convert.build_import(payload)
    refs = [node.node("blender_reference") for node in nodes]
    # "Props" is the embedded import: _1, not Houdini's trailing-number bump.
    assert [node.name() for node in nodes] == ["Props_1", "Lamp_1"]
    assert refs[0].evalParm("ref_target_kind1") == "collection" and refs[1].evalParm("ref_target1") == "Lamp_1"
    assert refs[0].evalParm("ref_include_disabled_collections1") == 1
    assert refs[1].evalParm("ref_include_disabled_collections1") is None
    assert refs[0].evalParm("file") == str(fake.blend).replace("\\", "/")
    assert calls == ["init", "bind", "refresh"] * 2


def test_cancel_and_untitled_scene_cache(fake):
    from cycles_houdini.blender import blend_convert

    fake.hou.scene.new = True
    snap = snapshot(fake)
    assert snap["asset_root"] == (fake.tmp / "cache" / "assets_blender").as_posix()  # untitled scene: the user cache
    assert snap["scene"] is None and any("not saved yet" in text for text in snap["notices"])
    cancel = threading.Event()

    def manifest_for(request):
        cancel.set()
        return scene_manifest()

    staging = []

    def evaluate(*args):
        staging.append(args[3])
        return fake_evaluate(manifest_for)(*args)

    with pytest.raises(blend_convert.ImportCancelled):
        blend_convert.prepare_import(snap, cancelled=cancel, evaluate=evaluate)
    assert staging and not Path(staging[0]).exists()  # the staging folder is removed


def test_import_blend_sync_wrapper_uses_the_worker(fake, monkeypatch):
    from cycles_houdini.blender import blend_convert, blend_worker

    seen = {}

    def evaluate_many(
        source, executable, requests, texture_directory=None, timeout=300, source_config=None, consume=None
    ):
        seen.update(source=source, executable=executable, timeout=timeout)
        return fake_evaluate(lambda request: scene_manifest())(
            source, executable, requests, texture_directory, timeout, source_config, consume
        )

    monkeypatch.setattr(blend_worker, "evaluate_many", evaluate_many)
    reports = []
    nodes = blend_convert.import_blend(
        str(fake.blend),
        [("collection", "Props")],
        structure="packed",
        materials="none",
        executable=sys.executable,
        timeout=42,
        report=lambda text, error=False: reports.append(text),
    )
    assert nodes[0].name() == "Props" and seen["timeout"] == 42 and seen["executable"] == sys.executable
    assert any("lossy" in text for text in reports)


def test_dialog_style_prepare_on_a_thread_without_hom(fake, monkeypatch):
    """The importer dialog calls prepare_import(source, selection, **options) off the host thread."""
    import hashlib

    from cycles_houdini.blender import blend_convert

    texture = b"PNG texture bytes"

    def manifest_for(request):
        manifest, arrays = scene_manifest()
        red = dict(MATERIALS[0])
        red["nodes"] = [
            dict(
                image=dict(
                    filename="textures/red_diff.png",
                    tiles=[],
                    textures="in_place",
                    packed=True,
                    missing=False,
                    dependencies=[
                        dict(filename="textures/red_diff.png", sha256=hashlib.sha256(texture).hexdigest(), packed=True)
                    ],
                )
            )
        ]
        manifest["materials"] = [red, *MATERIALS[1:]]
        return manifest, arrays

    def evaluate(source, executable, requests, texture_directory, timeout, source_config=None, consume=None):
        Path(texture_directory, "request-0", "textures").mkdir(parents=True, exist_ok=True)
        Path(texture_directory, "request-0", "textures", "red_diff.png").write_bytes(texture)
        return fake_evaluate(manifest_for)(
            source, executable, requests, texture_directory, timeout, source_config, consume
        )

    class NoHom(ModuleType):
        def __getattr__(self, name):
            raise AssertionError("HOM used on the background thread: hou." + name)

    host_hou = sys.modules["hou"]
    reports, result = [], {}

    def work():
        try:
            result["payload"] = blend_convert.prepare_import(
                str(fake.blend),
                [("collection", "Props")],
                storage="files",
                structure="packed",
                materials="link",
                frames=None,
                include_disabled=False,
                asset_root="$HIP/geo/assets_blender",
                executable=sys.executable,
                cancelled=threading.Event(),
                report=lambda text, error=False: reports.append((text, error)),
                evaluate=evaluate,
            )
        except BaseException as error:  # surfaced below
            result["error"] = error

    monkeypatch.setitem(sys.modules, "hou", NoHom("hou"))
    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    monkeypatch.setitem(sys.modules, "hou", host_hou)
    assert "error" not in result, result.get("error")
    payload = result["payload"]
    assert payload["snapshot"]["asset_root_abs"] is None  # expanded on the host, in build_import
    nodes = blend_convert.build_import(payload)
    # The dialog's old default asset root ($HIP/geo/assets_blender) reads as $HIP/assets_blender.
    assert nodes[0].node("file1").evalParm("file") == "$HIP/assets_blender/props/geo/Props/Props.bgeo.sc"
    copied = fake.assets / "tex" / "red_diff.png"
    assert copied.read_bytes() == texture  # packed images are placed when the material is built
    image = import_json(fake)["images"]["packed:" + hashlib.sha256(texture).hexdigest()]
    assert image["file"] == "red_diff.png" and image["packed"] is True
    assert any("lossy" in text for text, _ in reports)  # build_import reports through the same callable


def test_free_child_name_never_bumps_trailing_numbers():
    from cycles_houdini.blender.blend_library_materials import free_child_name

    class Parent:
        def __init__(self, names):
            self.names = set(names)

        def node(self, name):
            return Child(name) if name in self.names else None

    class Child:
        def __init__(self, name):
            self._name = name

        def path(self):
            return "/obj/" + self._name

    assert free_child_name(Parent([]), "tree_LOD1") == "tree_LOD1"
    assert free_child_name(Parent(["tree_LOD1"]), "tree_LOD1") == "tree_LOD1_1"
    assert free_child_name(Parent(["island_tree_01", "island_tree_01_1"]), "island_tree_01") == "island_tree_01_2"
    # A node keeping its own name is not a clash.
    assert free_child_name(Parent(["mat"]), "mat", Child("mat")) == "mat"
    # Fakes without child lookup keep the plain name.
    assert free_child_name(object(), "x") == "x"


def test_hierarchy_applies_collection_offset_to_objects_not_geometry():
    from cycles_houdini.blender import blend_convert as bc

    manifest, arrays = scene_manifest()
    names = {"kRed": "Red Paint", "kBlue": "Blue"}
    plain = {o["name"]: o for o in bc.assemble(manifest, arrays, "hierarchy", names, bc.Report())["objects"]}
    # A collection instance offset of Blender (3, 0, 1) is in every instance transform
    # (C * T(-offset) * M), not in the objects' own matrix_world.
    shift = np.eye(4)
    shift[:3, 3] = (-3, 0, -1)
    for instance in manifest["instances"]:
        old = np.asarray(instance["transform"], dtype=float).reshape(4, 4)
        instance["transform"] = (C @ shift @ np.linalg.inv(C) @ old).reshape(-1).tolist()
    moved = {o["name"]: o for o in bc.assemble(manifest, arrays, "hierarchy", names, bc.Report())["objects"]}
    for name, entry in plain.items():
        if entry["geo"] is not None:  # geometry is not shifted: the objects are
            assert np.allclose(moved[name]["geo"].P, entry["geo"].P)
        if entry["parent"]:
            assert np.allclose(moved[name]["local"], entry["local"])
        else:  # Houdini axes: Blender (-3, 0, -1) -> (-3, -1, 0)
            assert np.allclose(moved[name]["world"][:3, 3] - entry["world"][:3, 3], [-3, -1, 0])


def test_hierarchy_keeps_repeated_geometry_nodes_instances_packed():
    from cycles_houdini.blender import blend_convert as bc

    names = {"kRed": "Red Paint", "kBlue": "Blue"}
    manifest, arrays = distinct_scene_manifest()
    tree_world = C @ translate(5, 0, 0)
    # Tree's geometry nodes output: two copies of one shape (instances) at depth 2 (member depth + 1).
    manifest["instances"][2:] = [
        instance("i2", tree_world @ translate(0, 1, 0), "Tree", "Tree", 2, mesh="mesh_000000"),
        instance("i3", tree_world @ translate(0, 2, 0), "Tree", "Tree", 2, mesh="mesh_000001"),
    ]
    tree = {o["name"]: o for o in bc.assemble(manifest, arrays, "hierarchy", names, bc.Report())["objects"]}["Tree"]
    assert len(tree["geo"].poly_counts) == 2 and len(tree["geo"].packed_children) == 2
    assert len(tree["children"]) == 1  # one shared prototype
    # A single dupli of the object's own geometry (e.g. a component split) is merged into it.
    manifest["instances"].pop()
    tree = {o["name"]: o for o in bc.assemble(manifest, arrays, "hierarchy", names, bc.Report())["objects"]}["Tree"]
    assert len(tree["geo"].poly_counts) == 4 and not tree["geo"].packed_children


def test_hierarchy_linked_duplicates_share_one_packed_disk_file(fake):
    def manifest_for(request):
        manifest, arrays = scene_manifest()
        manifest["instances"] = manifest["instances"][:2]  # Child and Tree: one identical mesh each
        return manifest, arrays

    run(fake, "hierarchy", manifest_for=manifest_for)
    children = {node.name(): node for node in fake.obj.children()}
    files = [children[name].node("file1") for name in ("Child", "Tree")]
    assert files[0].evalParm("file") == files[1].evalParm("file")
    assert files[0].evalParm("file").endswith("/Props/hierarchy/shapes/Child.bgeo.sc")
    assert all(node.evalParm("loadtype") == "delayed" for node in files)
    folder = fake.assets / "geo" / "Props"
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.bgeo.sc")) == [
        "hierarchy/shapes/Child.bgeo.sc"
    ]
    assert import_json(fake)["names"]["shapes"]["Props"] == {"Child": "Child"}
    shape = file_text(folder / "hierarchy" / "shapes" / "Child.bgeo.sc")["content"]
    assert "prim:shop_materialpath" in shape["attribs"]  # bindings inside: no Material SOP reaches them
    assert children["Child"].node("material1") is None
    # Frame ranges keep one file per object (object motion lives in the per-frame geometry).
    run(fake, "hierarchy", frames=(1, 2, 1), manifest_for=manifest_for)
    assert (folder / "hierarchy" / "Child.0001.bgeo.sc").is_file()
    # The shared shape file of the single-frame import was replaced, so it is deleted.
    assert not (folder / "hierarchy" / "shapes").exists()


SUBSURF = dict(
    type="catmull_clark", level=2, adaptive=False, boundary="edge_only", fvar="corners_only", use_creases=True
)


def subdivided_record(key="mesh_000002"):
    record, arrays = mesh_record(key, "Smooth")
    record["subdivision"] = dict(SUBSURF, modifier="Subdivision")
    record["subdivision_viewport_level"] = 1
    arrays[key + "/ce"] = np.array([0, 1, 0, 0, 0, 0, 0], np.float32)  # edge (1, 2)
    arrays[key + "/cv"] = np.array([0, 0, 0, 0, 1, 0], np.float32)  # point 4
    record["attributes"] += [
        dict(name="crease_edge", domain="EDGE", data_type="FLOAT", array=key + "/ce"),
        dict(name="crease_vert", domain="POINT", data_type="FLOAT", array=key + "/cv"),
    ]
    return record, arrays


def test_unapplied_subsurf_keeps_the_cage_settings_and_creases():
    from cycles_houdini.blender import blend_convert as bc

    record, arrays = subdivided_record()
    geo = bc.mesh_geo(record, arrays, ["kRed", "kDefault", "kBlue"], {}, bc.Report())
    assert geo.subdivision == dict(
        type="catmull_clark", render_level=2, ipr_level=1, boundary="edge_only", fvar="corners_only", use_creases=True
    )
    # Houdini order after the winding flip: quad 0,3,2,1 and triangle 1,2,4; a vertex's creaseweight
    # is the edge to the next corner, so edge (1, 2) is quad corner 2 and triangle corner 0 (x10).
    assert geo.attribs["creaseweight"].values[:7, 0].tolist() == [0, 0, 10, 0, 10, 0, 0]
    assert geo.attribs["cornerweight"].values[:, 0].tolist() == [0, 0, 0, 0, 10, 0]
    assert "crease_edge" not in geo.attribs and "crease_vert" not in geo.attribs


def test_apply_subdivision_option_bakes_it_in_blender(fake):
    calls = []
    run(fake, "packed", calls=calls, apply_subdivision=True)
    assert calls[0][0]["native_subdivision"] is False


def subdivided_scene():
    """Child (the Smooth cage, Subsurf level 2) and Tree (plain) in one collection."""
    manifest, arrays = scene_manifest()
    record, extra = subdivided_record()
    arrays.update(extra)
    manifest["meshes"].append(record)
    manifest["instances"][0]["mesh_key"] = "mesh_000002"
    return manifest, arrays


def _subdivision_parms(node):
    return {
        name[len("cycles_subdivision_") :]: node.evalParm(name)
        for name in [parm.name for parm in node.parms()]
        if name.startswith("cycles_subdivision_")
    }


def test_packed_splits_objects_by_render_time_subdivision(fake):
    for structure in ("packed",):
        nodes, _, messages = run(fake, structure, manifest_for=lambda request: subdivided_scene())
        plain, smooth = nodes
        assert smooth.name().startswith("Props_subdiv") and "subdiv" not in plain.name()
        assert _subdivision_parms(plain) == {}
        assert _subdivision_parms(smooth) == dict(
            override=1,
            type="catmull_clark",
            adaptive=0,
            ipr=1,
            ipr_level=1,
            render_level=2,
            boundary="edge_only",
            fvar="corners_only",
            use_creases=1,
        )
        stems = [node.node("file1").evalParm("file").rsplit("/", 1)[1] for node in nodes]
        suffix = ""
        assert stems == [f"Props{suffix}.bgeo.sc", f"Props{suffix}_subdiv.bgeo.sc"]
        assert any("one per render-time subdivision setting" in text for text, _ in messages)


def test_hierarchy_sets_subdivision_on_the_object(fake):
    run(fake, "hierarchy", materials="none", manifest_for=lambda request: subdivided_scene())
    children = {node.name(): node for node in fake.obj.children()}
    assert _subdivision_parms(children["Child"])["render_level"] == 2
    assert _subdivision_parms(children["Tree"]) == {}


def test_particle_hair_is_named_in_the_import_log_and_report(fake):
    """blend_export reports legacy particle hair a conversion drops (task H2): the import log names
    the emitters once, the item report lists it among the skipped items."""

    def manifest_for(request):
        manifest, arrays = scene_manifest()
        manifest["diagnostics"] = list(manifest.get("diagnostics", [])) + [
            dict(
                code="blend.particle_hair",
                severity="warning",
                source="Child",
                message="Particle hair is not converted: only the emitter mesh is imported. Emitters: Child",
            )
        ]
        return manifest, arrays

    _, payload, messages = run(fake, "packed", manifest_for=manifest_for)
    logged = [text for text, _ in messages if "Particle hair is not converted" in text]
    assert logged == ["Props: Particle hair is not converted: only the emitter mesh is imported. Emitters: Child"]
    (item,) = payload["items"]
    assert any(text.startswith("Child: Particle hair is not converted") for text in item["report"]["skipped"])
