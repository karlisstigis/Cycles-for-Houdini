# SPDX-License-Identifier: Apache-2.0
"""Run in Blender: native control topology and conservative evaluated fallbacks."""

import argparse
from support.package_loader import import_blender
import json
from pathlib import Path
import sys
import bpy
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]
exporter = import_blender("blend_export")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
obj.name = "Subdivided"
for face in obj.data.polygons:
    face.use_smooth = True
# A render-only modifier before Subsurf must be included in the control mesh.
prior = obj.modifiers.new("Render only shrink", "SMOOTH")
prior.show_viewport = False
prior.factor = 0.1
sub = obj.modifiers.new("Last render Subsurf", "SUBSURF")
sub.render_levels = 3
sub.levels = 1
later = obj.modifiers.new("Viewport only triangulate", "TRIANGULATE")
later.show_render = False
crease = obj.data.attributes.new("crease_edge", "FLOAT", "EDGE")
crease.data[0].value = 1
corner = obj.data.attributes.new("crease_vert", "FLOAT", "POINT")
corner.data[0].value = 1
source = out / "subdivision.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(source))
checks = []


def run(name, native=True, expected=True, minimum_triangles=13):
    manifest = exporter.export(
        dict(source=str(source), selector_type="OBJECT", selector=obj.name, frame=1, native_subdivision=native),
        out / name,
    )
    record = manifest["meshes"][0]
    assert bool(record.get("subdivision")) == expected, (name, record, manifest["diagnostics"])
    assert sub.show_render, "Exporter did not restore source modifier state"
    with np.load(out / name / "geometry.npz", allow_pickle=False) as archive:
        size = sum(value.nbytes for value in archive.values())
        if expected:
            assert record["polygon_count"] == 6 and record["vertex_count"] == 8, record
            assert not {"normals", "triangle_vertices", "triangle_loops", "triangle_polygons"} & record["arrays"].keys()
            positions = archive[record["arrays"]["positions"]]
            assert np.max(np.abs(positions)) < 1, "Render-only modifier before Subsurf was omitted"
        else:
            assert record["triangle_count"] >= minimum_triangles, (name, record)
    row = dict(
        name=name,
        vertices=record["vertex_count"],
        polygons=record["polygon_count"],
        native=expected,
        array_bytes=size,
        diagnostics=manifest["diagnostics"],
    )
    checks.append(row)
    return row


native = run("native")
evaluated = run("evaluated", False, False)
assert native["array_bytes"] * 10 < evaluated["array_bytes"], (native, evaluated)
obj.data.polygons[0].use_smooth = False
run("flat-fallback", expected=False)
obj.data.polygons[0].use_smooth = True
crease.data[0].value = 0.5
run("fractional-crease-fallback", expected=False)
crease.data[0].value = 1
sub.quality = 4
run("quality-fallback", expected=False)
sub.quality = 3
sub.use_limit_surface = False
run("finite-surface-fallback", expected=False)
sub.use_limit_surface = True
field = obj.data.attributes.new("PaintedField", "FLOAT", "POINT")
run("attribute-fallback", expected=False)
obj.data.attributes.remove(field)
# A second object consuming this mesh must continue seeing the Subsurf result.
other_mesh = obj.data.copy()
other = bpy.data.objects.new("Consumer", other_mesh)
bpy.context.scene.collection.objects.link(other)
boolean = other.modifiers.new("Dependency", "BOOLEAN")
boolean.object = obj
run("dependency-fallback", expected=False)
bpy.data.objects.remove(other, do_unlink=True)
collection = bpy.data.collections.new("Consumed Collection")
bpy.context.scene.collection.children.link(collection)
collection.objects.link(obj)
tree = bpy.data.node_groups.new("Collection geometry consumer", "GeometryNodeTree")
info = tree.nodes.new("GeometryNodeCollectionInfo")
info.inputs["Collection"].default_value = collection
run("collection-dependency-fallback", expected=False)
bpy.data.node_groups.remove(tree)
instance = bpy.data.objects.new("Ordinary collection instance", None)
instance.instance_type = "COLLECTION"
instance.instance_collection = collection
bpy.context.scene.collection.objects.link(instance)
run("collection-instance-native")
bpy.data.objects.remove(instance, do_unlink=True)
collection.objects.unlink(obj)
bpy.data.collections.remove(collection)
sub.keyframe_insert(data_path="show_render", frame=1)
sub.show_render = False
sub.keyframe_insert(data_path="show_render", frame=2)
bpy.context.scene.frame_set(1)
run("animated-modifier-fallback", expected=False)
obj.animation_data_clear()
sub.show_render = True
bpy.context.scene.render.use_simplify = True
bpy.context.scene.render.simplify_subdivision_render = 1
row = run("simplified-native")
manifest = json.loads((out / "simplified-native/manifest.json").read_text())
assert manifest["meshes"][0]["subdivision"]["level"] == 1
bpy.context.scene.render.simplify_subdivision_render = 0
run("simplified-zero-evaluated", expected=False, minimum_triangles=12)
bpy.context.scene.render.use_simplify = False
# Save the unmodified fixture for native host render comparison.
bpy.ops.wm.save_as_mainfile(filepath=str(source))
(out / "report.json").write_text(json.dumps(dict(result="passed", checks=checks), indent=2))
print("SUBDIVISION_EXPORT_PASSED", json.dumps(checks))
