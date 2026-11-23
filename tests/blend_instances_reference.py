# SPDX-License-Identifier: Apache-2.0
"""Create an animated .blend fixture and render its native Blender Cycles references."""

import argparse
import json
import math
import os
from pathlib import Path
import struct
import sys
import zlib

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.devices import device_pci_bus_id, match_reference_optix_device


def png(path, colors):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = b"".join(b"\0" + bytes(v for rgb in row for v in rgb) for row in colors)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def material(path, packed):
    mat = bpy.data.materials.new(path.stem)
    mat.use_nodes = True
    tree = mat.node_tree
    output = tree.nodes.get("Material Output")
    shader = tree.nodes.get("Principled BSDF") if packed else tree.nodes.new("ShaderNodeEmission")
    if packed:
        shader.inputs["Emission Strength"].default_value = 1
        shader.inputs["Base Color"].default_value = (0, 0, 0, 1)
        shader.inputs["Specular IOR Level"].default_value = 0
    else:
        tree.links.new(shader.outputs[0], output.inputs["Surface"])
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(str(path))
    tex.interpolation = "Closest"
    if packed:
        tex.image.pack()
        tex.image.filepath = "//only_inside_the_blend.png"
    else:
        tex.image.filepath = "//" + path.name
    tree.links.new(tex.outputs["Color"], shader.inputs["Emission Color" if packed else "Color"])
    return mat


def panel(name, collection, x0, x1, height, materials):
    mesh = bpy.data.meshes.new(name)
    mid = (x0 + x1) / 2
    mesh.from_pydata([(x, 0, z) for z in (-height, height) for x in (x0, mid, x1)], [], [(0, 1, 4, 3), (1, 2, 5, 4)])
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    uv = mesh.uv_layers.new(name="UVMap")
    for slot, face in enumerate(mesh.polygons):
        mesh.materials.append(materials[slot])
        face.material_index = slot
        for loop, coord in zip(face.loop_indices, ((0, 0), (1, 0), (1, 1), (0, 1))):
            uv.data[loop].uv = coord
    return obj


parser = argparse.ArgumentParser()
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--device", choices=("CPU", "OPTIX"), required=True)
parser.add_argument("--optix-device-name")
parser.add_argument("--optix-device-pci")
parser.add_argument("--host-device-id")
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
dest = args.out.resolve()
dest.mkdir(exist_ok=True, parents=True)
asset = dest / "animated panels.blend"
if args.device == "CPU" or not asset.exists():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    group = bpy.data.collections.new("Panel Set")
    scene.collection.children.link(group)
    external, packed = dest / "external.png", dest / "packed.png"
    png(external, [[(200, 50, 10), (20, 185, 60)], [(30, 60, 225), (210, 190, 65)]])
    png(packed, [[(210, 35, 160), (25, 200, 180)], [(90, 35, 215), (215, 100, 30)]])
    mats = (material(external, False), material(packed, True))
    actor = panel("Hero Panel", group, -1.32, 0.42, 0.6, mats)
    other = panel("Side Panel", group, 0.60, 1.23, 0.38, mats[::-1])
    other.location.z = -0.45
    actor.shape_key_add(name="Basis")
    shape = actor.shape_key_add(name="Bend")
    shape.data[5].co.z += 0.5
    shape.value = 0
    shape.keyframe_insert(data_path="value", frame=1)
    shape.value = 1
    shape.keyframe_insert(data_path="value", frame=9)
    actor.location.z = -0.18
    actor.keyframe_insert(data_path="location", frame=1)
    actor.location.z = 0.28
    actor.keyframe_insert(data_path="location", frame=9)
    camera = bpy.data.cameras.new("Reference")
    obj = bpy.data.objects.new("Reference", camera)
    scene.collection.objects.link(obj)
    obj.location = (0, -5, 0)
    obj.rotation_euler = (math.pi / 2, 0, 0)
    camera.type = "ORTHO"
    camera.ortho_scale = 4
    scene.camera = obj
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 128
    scene.cycles.use_adaptive_sampling = scene.cycles.use_denoising = False
    scene.cycles.pixel_filter_type = "BOX"
    scene.cycles.filter_width = 1
    scene.render.film_transparent = True
    scene.render.resolution_x = scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.render.image_settings.color_mode = "RGBA"
    scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=str(asset), relative_remap=False)
else:
    bpy.ops.wm.open_mainfile(filepath=str(asset), load_ui=False, use_scripts=False)
scene = bpy.context.scene
device = "CPU"
selected_device_id = "CPU"
selected_device_name = "CPU"
if args.device == "OPTIX":
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.refresh_devices()
    candidates = [d for d in prefs.devices if d.type == "OPTIX"]
    requested_host_id = args.host_device_id or os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE")
    requested_name = args.optix_device_name
    requested_pci = args.optix_device_pci or device_pci_bus_id(requested_host_id or "")
    if requested_host_id and not requested_name:
        names = {item.name for item in candidates if item.name.casefold() in requested_host_id.casefold()}
        assert len(names) == 1, (
            "Cannot map selected host GPU to a unique Blender device name",
            requested_host_id,
            names,
        )
        requested_name = names.pop()
    if requested_name:
        selected = match_reference_optix_device(
            [dict(id=item.id, name=item.name, backend=item.type) for item in candidates],
            name=requested_name,
            pci=requested_pci,
        )
        chosen = next(item for item in candidates if item.id == selected["id"])
    else:
        chosen = next((item for item in candidates if "4090" in item.name), candidates[0])
    for d in prefs.devices:
        d.use = d.id == chosen.id
    device = chosen.name
    selected_device_id, selected_device_name = chosen.id, chosen.name
    assert [item.id for item in prefs.devices if item.use] == [chosen.id]
scene.cycles.device = "GPU" if args.device == "OPTIX" else "CPU"
for form in ("collection", "object"):
    bpy.data.objects["Side Panel"].hide_render = form == "object"
    for frame in (1, 7.5):
        scene.frame_set(int(frame), subframe=frame % 1)
        scene.render.filepath = str(dest / f"blender-{args.device}-{form}-{frame:g}.exr")
        bpy.ops.render.render(write_still=True)
(dest / f"blender-{args.device}.json").write_text(
    json.dumps(
        dict(
            version=bpy.app.version_string,
            commit=bpy.app.build_hash.decode(),
            device=device,
            backend=args.device,
            device_id=selected_device_id,
            device_name=selected_device_name,
            pci_bus_id=device_pci_bus_id(selected_device_id),
            host_device_id=args.host_device_id
            or (os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE") if args.device == "OPTIX" else None),
            frames=[1, 7.5],
        ),
        indent=2,
    )
)
