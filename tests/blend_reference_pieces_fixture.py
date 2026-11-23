# SPDX-License-Identifier: Apache-2.0
"""Create two tiny smooth, animated subdivision assets; never renders or uses a GPU."""

import argparse
import json
from pathlib import Path
import sys
import bpy

parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
args.out.mkdir(parents=True, exist_ok=True)
records = []
for name, filename, color, aspect, direction in (
    ("Smooth Red", "red source.blend", (0.8, 0.025, 0.015, 1), (1, 0.7, 1), 1),
    ("Smooth Blue", "blue override.blend", (0.015, 0.04, 0.9, 1), (1.3, 0.7, 0.8), -1),
    ("Changing Topology", "topology source.blend", (0.8, 0.025, 0.015, 1), (1, 0.7, 1), 1),
):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_cube_add(size=1)
    obj = bpy.context.object
    obj.name = name
    for vertex in obj.data.vertices:
        vertex.co.x *= aspect[0]
        vertex.co.y *= aspect[1]
        vertex.co.z *= aspect[2]
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    obj.shape_key_add(name="Basis")
    shape = obj.shape_key_add(name="Animated Bend")
    for vertex in shape.data:
        if vertex.co.z > 0:
            vertex.co.x += 0.35 * direction
            vertex.co.z += 0.5
    for frame, value in ((1, 0), (9, 1)):
        shape.value = value
        shape.keyframe_insert(data_path="value", frame=frame)
        obj.location.x = direction * (-0.32 + 0.64 * value)
        obj.keyframe_insert(data_path="location", frame=frame)
    if name == "Changing Topology":
        array = obj.modifiers.new("Changing Vertex Population", "ARRAY")
        for frame, count in ((3, 1), (4, 2)):
            array.count = count
            array.keyframe_insert(data_path="count", frame=frame)
    modifier = obj.modifiers.new("Native Smooth Subdivision", "SUBSURF")
    modifier.subdivision_type = "CATMULL_CLARK"
    modifier.levels = modifier.render_levels = 2
    material = bpy.data.materials.new(name + " Emission")
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = 1
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    obj.data.materials.append(material)
    scene.frame_set(1)
    destination = args.out / filename
    bpy.ops.wm.save_as_mainfile(filepath=str(destination), relative_remap=False)
    records.append(dict(name=name, file=str(destination), color=list(color[:3])))
# A shared library exercises the new interface without hidden file overrides.
bpy.ops.wm.read_factory_settings(use_empty=True)
shared_collection = bpy.data.collections.new("Shared Pieces")
bpy.context.scene.collection.children.link(shared_collection)
for record in records[:2]:
    with bpy.data.libraries.load(record["file"], link=False) as (available, loaded):
        loaded.objects = [record["name"]]
    for obj in loaded.objects:
        shared_collection.objects.link(obj)
shared = args.out / "shared pieces.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(shared), relative_remap=False)
for record in records[:2]:
    record["shared_file"] = str(shared)
    record["shared_collection"] = "Shared Pieces"
(args.out / "pieces-fixture.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
