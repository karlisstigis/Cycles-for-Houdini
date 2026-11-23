# SPDX-License-Identifier: Apache-2.0
"""Fresh Blender references for flat/smooth nonplanar polygon import.

blender -b --factory-startup --python-exit-code 1 --python tests/blender_mesh_shading_reference.py -- --out <directory>
"""

import argparse, json, shutil, sys
from support.package_loader import import_blender
from pathlib import Path
import bpy
import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--artist-source", type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
args.out.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]
exporter = import_blender("blend_export")
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 16
scene.render.resolution_x = 96
scene.render.resolution_y = 96
scene.render.resolution_percentage = 100
scene.render.film_transparent = True
scene.render.image_settings.file_format = "OPEN_EXR"
scene.render.image_settings.color_depth = "32"
scene.cycles.use_denoising = False
camera_data = bpy.data.cameras.new("Camera")
camera_data.type = "ORTHO"
camera_data.ortho_scale = 3
camera = bpy.data.objects.new("Camera", camera_data)
scene.collection.objects.link(camera)
camera.location.z = 5
scene.camera = camera
material = bpy.data.materials.new("EncodedNormal")
material.use_nodes = True
nodes = material.node_tree.nodes
nodes.clear()
links = material.node_tree.links
geometry = nodes.new("ShaderNodeNewGeometry")
half = nodes.new("ShaderNodeVectorMath")
half.operation = "SCALE"
half.inputs["Scale"].default_value = 0.5
add = nodes.new("ShaderNodeVectorMath")
add.operation = "ADD"
add.inputs[1].default_value = (0.5, 0.5, 0.5)
emission = nodes.new("ShaderNodeEmission")
output = nodes.new("ShaderNodeOutputMaterial")
links.new(geometry.outputs["Normal"], half.inputs[0])
links.new(half.outputs["Vector"], add.inputs[0])
links.new(add.outputs["Vector"], emission.inputs["Color"])
links.new(emission.outputs[0], output.inputs["Surface"])
positions = [(-1, -1, 0), (1, -1, 0), (1, 1, 1.2), (-1, 1, 0), (-1, 1.7, -0.5), (1, 1.7, 0.4)]
mesh = bpy.data.meshes.new("WarpedQuads")
mesh.from_pydata(positions, [], [(0, 1, 2, 3), (3, 2, 5, 4)])
uv = mesh.uv_layers.new(name="UVMap")
for loop in mesh.loops:
    point = mesh.vertices[loop.vertex_index].co
    uv.data[loop.index].uv = ((point.x + 1) * 0.5, (point.y + 1) / 3)
obj = bpy.data.objects.new("WarpedQuads", mesh)
scene.collection.objects.link(obj)
mesh.materials.append(material)
report = []
for kind, smooth in [
    ("flat", (False, False)),
    ("smooth", (True, True)),
    ("mixed", (False, True)),
    ("custom-flat", (False, False)),
]:
    for polygon, value in zip(mesh.polygons, smooth):
        polygon.use_smooth = value
    if kind == "custom-flat":
        mesh.normals_split_custom_set([(0, 0, 1)] * len(mesh.loops))
    mesh.update()
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    evaluated_mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get())
    arrays = {}
    record = exporter._mesh_arrays(evaluated_mesh, "mesh", arrays)
    evaluated.to_mesh_clear()
    np.savez(args.out / (kind + ".npz"), **arrays)
    (args.out / (kind + ".json")).write_text(json.dumps(record, indent=2))
    scene.render.filepath = str((args.out / (kind + ".exr")).resolve())
    bpy.ops.render.render(write_still=True)
    report.append(dict(kind=kind, smooth=list(smooth), triangles=record["triangle_count"]))
if args.artist_source:
    # Read the artist source, export through its render dependency graph, and
    # render its actual modifiers independently. Never save the source file.
    for frame in (0, 15, 20, 25, 30):
        kind = "artist-" + str(frame)
        bundle = args.out / (kind + "-bundle")
        manifest = exporter.export(
            dict(source=str(args.artist_source.resolve()), selector_type="OBJECT", selector="Shape", frame=frame),
            bundle,
        )
        (args.out / (kind + ".json")).write_text(json.dumps(manifest["meshes"][0], indent=2))
        with np.load(bundle / "geometry.npz", allow_pickle=False) as data:
            np.savez(args.out / (kind + ".npz"), **{key: data[key] for key in data.files})
        scene = bpy.context.scene
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 16
        scene.cycles.use_denoising = False
        scene.render.resolution_x = 96
        scene.render.resolution_y = 96
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = True
        scene.render.image_settings.file_format = "OPEN_EXR"
        scene.render.image_settings.color_depth = "32"
        camera_data = bpy.data.cameras.new("ShadingCheckCamera")
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = 3
        camera = bpy.data.objects.new("ShadingCheckCamera", camera_data)
        scene.collection.objects.link(camera)
        camera.location.z = 5
        scene.camera = camera
        material = bpy.data.materials.new("EncodedNormal")
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        links = material.node_tree.links
        geometry = nodes.new("ShaderNodeNewGeometry")
        half = nodes.new("ShaderNodeVectorMath")
        half.operation = "SCALE"
        half.inputs["Scale"].default_value = 0.5
        add = nodes.new("ShaderNodeVectorMath")
        add.operation = "ADD"
        add.inputs[1].default_value = (0.5, 0.5, 0.5)
        emission = nodes.new("ShaderNodeEmission")
        output = nodes.new("ShaderNodeOutputMaterial")
        links.new(geometry.outputs["Normal"], half.inputs[0])
        links.new(half.outputs["Vector"], add.inputs[0])
        links.new(add.outputs["Vector"], emission.inputs["Color"])
        links.new(emission.outputs[0], output.inputs["Surface"])
        obj = bpy.data.objects["Shape"]
        obj.data.materials.clear()
        obj.data.materials.append(material)
        scene.render.filepath = str((args.out / (kind + ".exr")).resolve())
        bpy.ops.render.render(write_still=True)
        report.append(dict(kind=kind, frame=frame))
        evaluated_kind = "artist-evaluated-" + str(frame)
        evaluated_bundle = args.out / (evaluated_kind + "-bundle")
        evaluated_manifest = exporter.export(
            dict(
                source=str(args.artist_source.resolve()),
                selector_type="OBJECT",
                selector="Shape",
                frame=frame,
                native_subdivision=False,
            ),
            evaluated_bundle,
        )
        (args.out / (evaluated_kind + ".json")).write_text(json.dumps(evaluated_manifest["meshes"][0], indent=2))
        with np.load(evaluated_bundle / "geometry.npz", allow_pickle=False) as data:
            np.savez(args.out / (evaluated_kind + ".npz"), **{key: data[key] for key in data.files})
        shutil.copyfile(args.out / (kind + ".exr"), args.out / (evaluated_kind + ".exr"))
        report.append(dict(kind=evaluated_kind, frame=frame))
(args.out / "reference.json").write_text(json.dumps(dict(blender=bpy.app.version_string, cases=report), indent=2))
