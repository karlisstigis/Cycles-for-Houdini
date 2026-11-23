# SPDX-License-Identifier: Apache-2.0
"""Converter-only exporter data (blend_export request flags) against a real Blender (bpy module).

Builds a small scene in a separate Python process with bpy: a mesh with vertex groups parented
to an empty, hair curves, and a geometry nodes object that outputs a point cloud plus geometry
instances. Checks the converter flags (vertex_groups, curves, pointclouds, object_tree,
material_export="names", frame="scene", material_textures="in_place") and that a render request
without them is unchanged.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.blender, pytest.mark.cpu]

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, sys, tempfile
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
from cycles_blender import blend_export

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.frame_set(7)
root = bpy.data.collections.new("Asset Root"); scene.collection.children.link(root)
child = bpy.data.collections.new("Sub Coll"); root.children.link(child)
me = bpy.data.meshes.new("QuadMesh")
me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0)], [(0, 4)], [(0, 1, 2, 3)])
me.uv_layers.new(name="UVMap")
quaternion = me.attributes.new("rotation", "QUATERNION", "POINT")
me.materials.append(bpy.data.materials.new("Red Paint"))
parent = bpy.data.objects.new("Parent Empty", None); child.objects.link(parent)
parent.location = (0, 0, 2)
obj = bpy.data.objects.new("Quad", me); child.objects.link(obj)
obj.parent = parent; obj.location = (1, 0, 0)
group = obj.vertex_groups.new(name="weights"); group.add([0, 2], 0.75, "REPLACE")
hair = bpy.data.hair_curves.new("Hair")
hair.add_curves([3, 4])
hair.attributes["position"].data.foreach_set("vector", [0, 0, 0, 0, 0, 1, 0, 0, 2, 1, 0, 0, 1, 0, 1, 1, 0, 2, 1, 0, 3])
radius = hair.attributes.new("radius", "FLOAT", "POINT"); radius.data.foreach_set("value", [0.1] * 7)
root.objects.link(bpy.data.objects.new("Hair Obj", hair))
base = bpy.data.meshes.new("GNBase"); base.from_pydata([(0, 0, 0), (3, 0, 0)], [], [])
gn = bpy.data.objects.new("GN Points", base); root.objects.link(gn)
gn.location = (0, 5, 0)
tree = bpy.data.node_groups.new("GN", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
nin, nout = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
points = tree.nodes.new("GeometryNodeMeshToPoints")
instancer = tree.nodes.new("GeometryNodeInstanceOnPoints")
info = tree.nodes.new("GeometryNodeObjectInfo"); info.inputs["Object"].default_value = obj
join = tree.nodes.new("GeometryNodeJoinGeometry")
tree.links.new(nin.outputs[0], points.inputs["Mesh"])
tree.links.new(nin.outputs[0], instancer.inputs["Points"])
tree.links.new(info.outputs["Geometry"], instancer.inputs["Instance"])
tree.links.new(points.outputs[0], join.inputs[0])
tree.links.new(instancer.outputs[0], join.inputs[0])
tree.links.new(join.outputs[0], nout.inputs[0])
gn.modifiers.new("GN", "NODES").node_group = tree
# A cube with a final Subsurf (render 2, viewport 1) and one creased edge, outside "Asset Root".
smooth_me = bpy.data.meshes.new("CubeMesh")
smooth_me.from_pydata(
    [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
    [],
    [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
)
smooth_me.update()
smooth_me.shade_smooth()  # flat faces make Blender evaluate the Subsurf itself
smooth_me.uv_layers.new(name="UVMap")
crease = smooth_me.attributes.new("crease_edge", "FLOAT", "EDGE")
crease.data[0].value = 1.0
smooth = bpy.data.objects.new("Cube Smooth", smooth_me); scene.collection.objects.link(smooth)
subsurf = smooth.modifiers.new("Subdivision", "SUBSURF")
subsurf.levels, subsurf.render_levels = 1, 2
bevel_data = bpy.data.curves.new("BevelCurve", "CURVE"); bevel_data.dimensions = "3D"
spline = bevel_data.splines.new("POLY"); spline.points.add(2)
for index, point in enumerate(spline.points):
    point.co = (index, 0, 0, 1)
bevel_data.bevel_depth = 0.1
scene.collection.objects.link(bpy.data.objects.new("Bevel Curve", bevel_data))
# Nested geometry nodes instances: 3 x 3 "branches" (random non-uniform scale), each 4 cubes,
# plus a plain mesh component on the instancer.
forest_mesh = bpy.data.meshes.new("ForestBase")
forest_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
forest = bpy.data.objects.new("Forest", forest_mesh); scene.collection.objects.link(forest)
forest.location = (10, 0, 0)
nested = bpy.data.node_groups.new("Nested", "GeometryNodeTree")
nested.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
nested.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
N, L = nested.nodes.new, nested.links.new
gin, gout = N("NodeGroupInput"), N("NodeGroupOutput")
grid = N("GeometryNodeMeshGrid"); grid.inputs["Vertices X"].default_value = 3; grid.inputs["Vertices Y"].default_value = 3
grid_points = N("GeometryNodeMeshToPoints"); L(grid.outputs[0], grid_points.inputs[0])
line = N("GeometryNodeMeshLine"); line.inputs["Count"].default_value = 4
line_points = N("GeometryNodeMeshToPoints"); L(line.outputs[0], line_points.inputs[0])
leaf = N("GeometryNodeMeshCube")
branch = N("GeometryNodeInstanceOnPoints"); L(line_points.outputs[0], branch.inputs["Points"]); L(leaf.outputs[0], branch.inputs["Instance"])
trees = N("GeometryNodeInstanceOnPoints"); L(grid_points.outputs[0], trees.inputs["Points"]); L(branch.outputs[0], trees.inputs["Instance"])
scale = N("FunctionNodeRandomValue"); scale.data_type = "FLOAT_VECTOR"
scale.inputs["Min"].default_value = (0.5, 0.5, 0.5); L(scale.outputs[0], trees.inputs["Scale"])
join_all = N("GeometryNodeJoinGeometry"); L(trees.outputs[0], join_all.inputs[0]); L(gin.outputs[0], join_all.inputs[0])
L(join_all.outputs[0], gout.inputs[0])
forest.modifiers.new("Nested", "NODES").node_group = nested
import os
os.makedirs(out + "/tex", exist_ok=True)
painted = bpy.data.images.new("painted", 2, 2)
painted.filepath_raw = out + "/tex/painted.png"
painted.file_format = "PNG"
painted.save()
textured_mesh = bpy.data.meshes.new("TexturedMesh")
textured_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
textured_material = bpy.data.materials.new("Textured")
textured_material.use_nodes = True
image_node = textured_material.node_tree.nodes.new("ShaderNodeTexImage")
image_node.image = painted
principled = next(n for n in textured_material.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
textured_material.node_tree.links.new(image_node.outputs["Color"], principled.inputs["Base Color"])
textured_mesh.materials.append(textured_material)
scene.collection.objects.link(bpy.data.objects.new("Textured", textured_mesh))
path = out + "/synthetic.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

flags = dict(vertex_groups=True, curves=True, pointclouds=True, object_tree=True, native_subdivision=False)
result = {}
with tempfile.TemporaryDirectory() as textures:
    requests = [
        dict(source=path, selector_type="COLLECTION", selector="Asset Root", frame="scene", **flags),
        dict(source=path, selector_type="OBJECT", selector="GN Points", frame=3.0, material_export="names", **flags),
        dict(source=path, selector_type="OBJECT", selector="Quad", frame=1.0),
        dict(source=path, selector_type="COLLECTION", selector="Asset Root", frame=1.0),
        dict(source=path, selector_type="OBJECT", selector="Cube Smooth", frame=1.0, **dict(flags, native_subdivision=True)),
        dict(source=path, selector_type="OBJECT", selector="Cube Smooth", frame=1.0, **flags),
        dict(source=path, selector_type="OBJECT", selector="Bevel Curve", frame=1.0, **flags),
        dict(source=path, selector_type="OBJECT", selector="Bevel Curve", frame=1.0),
        dict(source=path, selector_type="OBJECT", selector="Forest", frame=1.0, **dict(flags, nested_instances=True)),
        dict(source=path, selector_type="OBJECT", selector="Textured", frame=1.0, material_textures="in_place", **flags),
        dict(source=path, selector_type="OBJECT", selector="Textured", frame=1.0),
    ]
    names = (
        "collection", "object", "render", "render_curves", "subsurf_native", "subsurf_applied", "bevel", "bevel_render",
        "forest", "textured_in_place", "textured_render",
    )
    for name, value in zip(names, blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = {"error": str(value)}
            continue
        manifest, arrays = value
        if name.startswith("textured"):
            result[name] = dict(
                images=[n["image"] for m in manifest["materials"] for n in m["nodes"] if "image" in n], out=out
            )
            continue
        result[name] = dict(
            manifest={k: v for k, v in manifest.items() if k not in ("dependencies",)},
            arrays={k: list(v.shape) for k, v in arrays.items()},
            vertex_group_values={
                g["name"]: arrays[g["array"]].tolist() for m in manifest["meshes"] for g in m.get("vertex_groups", [])
            },
        )
        if name == "forest":
            import numpy as np
            from cycles_houdini.blender import blend_convert

            def world_points(geo, children, matrix):
                used = geo.P[np.unique(geo.poly_points)] if len(geo.poly_counts) else np.zeros((0, 3))
                points = [np.c_[used, np.ones(len(used))] @ matrix.T]
                for index, key in enumerate(geo.packed_children):
                    local = np.array(geo.packed_matrices[index]); local[:3, 3] = geo.P[int(geo.packed_points[index])]
                    points += world_points(children[key], children, matrix @ local)
                return points

            summary = {}
            for nested_mode in (True, False):
                frame = blend_convert.assemble(
                    manifest, arrays, "packed", {}, blend_convert.Report(), dict(nested_instances=nested_mode)
                )
                geo, children = frame["geo"], frame["children"]
                top = [children[key] for key in geo.packed_children]
                points = np.concatenate(world_points(geo, children, np.eye(4)))[:, :3]
                summary[str(nested_mode)] = dict(
                    top=len(geo.packed_children),
                    inner=sorted(len(child.packed_children) for child in top),
                    unique=len(set(geo.packed_children)),
                    points=np.round(points[np.lexsort(np.round(points, 4).T)], 4).tolist(),
                )
            hierarchy = blend_convert.assemble(manifest, arrays, "hierarchy", {}, blend_convert.Report(), {})
            [forest] = hierarchy["objects"]
            summary["hierarchy"] = dict(top=len(forest["geo"].packed_children), polys=len(forest["geo"].poly_counts))
            result[name]["nesting"] = summary
            result[name]["trees"] = list(manifest.get("instance_trees", {}))
        if name.startswith("subsurf"):
            from cycles_houdini.blender import blend_convert
            [mesh] = manifest["meshes"]
            geo = blend_convert.mesh_geo(mesh, arrays, [], {}, blend_convert.Report())
            result[name]["geo"] = dict(
                subdivision=geo.subdivision,
                points=int(geo.num_points),
                crease=sorted(float(v) for v in geo.attribs["creaseweight"].values[:, 0]) if "creaseweight" in geo.attribs else None,
            )
print("RESULT:" + json.dumps(result, default=str))
"""


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_convert_export")
    script = folder / "make_and_export.py"
    script.write_text(SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT / "houdini/python3.13libs"), str(folder)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


def test_collection_converter_export(exported):
    manifest = exported["collection"]["manifest"]
    assert manifest["converter"] is True and manifest["frame"] == 7.0  # frame="scene": the saved frame
    objects = {entry["name"]: entry for entry in manifest["objects"]}
    assert objects["Quad"]["parent"] == "Parent Empty" and objects["Parent Empty"]["type"] == "EMPTY"
    assert objects["Quad"]["collection_path"] == ["Asset Root", "Sub Coll"]
    assert objects["Quad"]["transform"][3] == 1.0 and objects["Quad"]["transform"][7] == 2.0  # C @ world
    instances = manifest["instances"]
    kinds = sorted((i["source_name"], i["geometry_type"], i["owner"]) for i in instances)
    assert ("Hair Obj", "curves", "Hair Obj") in kinds and ("GN Points", "pointcloud", "GN Points") in kinds
    # Geometry nodes geometry instances carry the instanced mesh (not the empty instancer mesh).
    generated = [i for i in instances if i["source_name"] == "GN Points" and i["depth"] > 2]
    meshes = {mesh["key"]: mesh for mesh in manifest["meshes"]}
    assert len(generated) == 2 and all(meshes[i["mesh_key"]]["vertex_count"] == 5 for i in generated)
    # The member index is the last persistent_id entry, so the owner is the generating member.
    assert all(i["owner"] == "GN Points" and i["dupli_path"][-1] == 1 for i in generated)
    [curves] = manifest["curves"]
    assert curves["curve_count"] == 2 and curves["point_count"] == 7 and "radius" in curves["arrays"]
    assert exported["collection"]["arrays"][curves["arrays"]["offsets"]] == [3]
    [cloud] = manifest["pointclouds"]
    assert cloud["point_count"] == 2
    quad = next(mesh for mesh in manifest["meshes"] if mesh["vertex_count"] == 5 and mesh.get("vertex_groups"))
    assert [g["name"] for g in quad["vertex_groups"]] == ["weights"]
    assert exported["collection"]["vertex_group_values"]["weights"] == [0.75, 0.0, 0.75, 0.0, 0.0]
    # Quaternions are imported since artist rule U5 (2026-11-13), no longer skipped.
    assert quad["skipped_attributes"] == []
    assert ("rotation", "POINT", "QUATERNION") in [(a["name"], a["domain"], a["data_type"]) for a in quad["attributes"]]
    red = next(record for record in manifest["materials"] if record["name"] == "Red Paint")
    assert red["local_name"] == "Red Paint" and red["library"] == "" and "nodes" in red


def test_object_converter_export_and_material_names(exported):
    manifest = exported["object"]["manifest"]
    assert manifest["frame"] == 3.0 and [entry["name"] for entry in manifest["objects"]] == ["GN Points"]
    assert all(i["owner"] == "GN Points" for i in manifest["instances"])
    assert all(not record["nodes"] for record in manifest["materials"])  # names only, no translation


def test_render_requests_are_unchanged(exported):
    manifest = exported["render"]["manifest"]
    assert not {"converter", "objects", "curves", "pointclouds"} & set(manifest)
    for instance in manifest["instances"]:
        assert not {"owner", "depth", "geometry_type"} & set(instance)
    assert all("vertex_groups" not in mesh and "skipped_attributes" not in mesh for mesh in manifest["meshes"])
    # Hair curves (task H1, test_blend_hair.py) and point clouds (task H2,
    # test_blend_pointcloud.py) render in a render reference with the meshes, without warnings.
    curves = exported["render_curves"]
    assert "error" not in curves, curves
    assert not [d for d in curves["manifest"]["diagnostics"] if d["code"] == "blend.unsupported_geometry"]
    assert curves["manifest"]["instances"]
    hair = [i for i in curves["manifest"]["instances"] if i.get("geometry_type") == "curves"]
    assert [i["name"] for i in hair] == ["Hair Obj"] and len(curves["manifest"]["hair"]) == 1
    points = [i for i in curves["manifest"]["instances"] if i.get("geometry_type") == "points"]
    assert [i["name"] for i in points] == ["GN Points"] and curves["manifest"]["points"][0]["point_count"] == 2


def test_final_subsurf_stays_a_cage_unless_applied(exported):
    native = exported["subsurf_native"]
    [mesh] = native["manifest"]["meshes"]
    assert mesh["subdivision"]["level"] == 2 and mesh["subdivision_viewport_level"] == 1
    geo = native["geo"]
    assert geo["points"] == 8 and geo["subdivision"]["render_level"] == 2 and geo["subdivision"]["ipr_level"] == 1
    # One creased edge, shared by two quads: two corners carry Houdini sharpness 10.
    assert geo["crease"].count(10.0) == 2 and sum(geo["crease"]) == 20.0
    applied = exported["subsurf_applied"]
    [mesh] = applied["manifest"]["meshes"]
    assert "subdivision" not in mesh and applied["geo"]["points"] > 8 and applied["geo"]["subdivision"] is None


def test_curve_objects_are_exported_once(exported):
    # Blender yields a curve object and its evaluated mesh component (a MESH dupli of itself);
    # only the mesh component is exported, by the converter and by references alike.
    for name in ("bevel", "bevel_render"):
        manifest = exported[name]["manifest"]
        assert len(manifest["instances"]) == 1 and len(manifest["meshes"]) == 1, name


def test_nested_geometry_nodes_instances_stay_nested(exported):
    nesting = exported["forest"]["nesting"]
    assert exported["forest"]["trees"] == ["Forest"]
    flat, nested = nesting["False"], nesting["True"]
    # Flat: the instancer triangle plus 9 x 4 cubes as top-level packed prims on one shape.
    assert flat["top"] == 1 + 36 and flat["unique"] == 2
    # Nested: the triangle plus 9 packed branches, all one prototype holding 4 packed cubes,
    # although every branch has its own random non-uniform scale.
    assert nested["top"] == 1 + 9 and nested["unique"] == 2
    assert sorted(nested["inner"])[-9:] == [4] * 9
    # Same world-space geometry either way.
    assert nested["points"] == flat["points"]
    # Hierarchy packs the 9 repeated branches in the object.
    assert nesting["hierarchy"] == {"top": 9, "polys": 1}


def test_converter_requests_record_textures_in_place(exported):
    # material_textures="in_place" (set by blend_convert.requests_for): the file stays where it is.
    textured = exported["textured_in_place"]
    [image] = textured["images"]
    assert image["filename"] == Path(textured["out"]).resolve().as_posix() + "/tex/painted.png"
    assert (image["textures"], image["packed"], image["missing"]) == ("in_place", False, False)
    assert all("sha256" not in dependency for dependency in image["dependencies"])  # never read


def test_reference_render_requests_still_copy_textures(exported):
    [image] = exported["textured_render"]["images"]
    assert image["filename"] == "textures/painted.png" and "packed" not in image and "textures" not in image
    assert len(image["dependencies"][0]["sha256"]) == 64
