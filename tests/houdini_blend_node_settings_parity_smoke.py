# SPDX-License-Identifier: Apache-2.0
"""Link-mode parity for Blender node settings that change pixels (Blender material import).

    hython tests/houdini_blend_node_settings_parity_smoke.py [--work DIR] [--report FILE] [--device CPU]
        [--blender EXE] [--threshold 0.1] [--scenes cells,sky,normals]

Two generated .blend scenes, each rendered by Blender 5.3's Cycles (the worker's executable) and by
Houdini with the collection imported in Link mode, through the same camera (helpers of
houdini_blend_sync_parity_smoke.py), compared region by region (mean of each of 4 x 4 regions):
1. "Cells", a 4 x 4 grid of 1.6-unit planes under a large emitter, one non-default node setting
   per cell (CELLS lists them, row by row from the top): Wave profile Saw; Brick offset, squash
   and their frequencies; Clamp type Range; Magic depth; Gabor type 3D; MixRGB Use Alpha (not
   read by Blender's Cycles); Combine Color HSV; Vector Rotate Euler; Metallic fresnel Physical
   Conductor; Mix in Float mode; Principled Hair Melanin and Absorption parametrizations (hair
   curves); Volume Scatter phase Rayleigh and Fournier-Forand (boxes); Math Fract; Map Range in
   vector mode with Smooth Step.
2. "Sky": a floor and a box lit only by a Sky Texture world (sun elevation 35 degrees, rotation
   120 degrees, size 2 degrees), imported as a Cycles World Material (blend_worlds, Link); the
   Sky node must also show the angles in degrees.
3. "Normals", the Cells grid with emitting planes whose strength is a Fresnel or Layer Weight
   output with a Normal Map (a strong constant tilt) or a Bump (wave bands) wired into Normal
   (NORMAL_CELLS), plus the same nodes with Normal unconnected (the shading normal).
Metric per region: emission cells (noise free) the mean absolute pixel difference, the lit cells
(metallic, hair, volume, sky) the largest per-channel difference of means, each relative to the
Blender region (0.002 absolute in dark regions). Controls prove the comparison sees each setting:
the imported native setting is put back to the native default (CONTROLS; the Sky angles back to
the radians the import wrote before this test existed; for Normals the Normal wire of the native
Fresnel and Layer Weight nodes taken out) and the re-render must then miss Blender in that region.
Prints a JSON summary; exits non-zero when a check failed.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import traceback
from pathlib import Path

import hou

sys.path.insert(0, str(Path(__file__).resolve().parent))
import houdini_blend_sync_parity_smoke as parity  # noqa: E402

SAMPLES = 128
SKY = dict(sun_elevation=35.0, sun_rotation=120.0, sun_size=2.0)
# Lit cells, compared by per-channel means (noisy); the others pixel by pixel.
NOISY = {"metallic_physical", "hair_melanin", "hair_absorption", "scatter_rayleigh", "scatter_fournier_forand"}
# Control renders: cell -> (native node type, {parameter: native default}); the native node's
# setting is put back to what an import that dropped or refused the Blender setting would leave.
# (MixRGB Use Alpha is not read by Blender's Cycles and has no parameter; Mix Float and Math
# Fract failed to import before, so no "dropped" state exists for them.)
CONTROLS = {
    "wave_saw": ("wave_texture", dict(profile="sine")),
    "brick_offset_squash": ("brick_texture", dict(offset=0.5, squash=1.0, offset_frequency=2, squash_frequency=2)),
    "clamp_range": ("clamp", dict(clamp_type="minmax")),
    "magic_depth": ("magic_texture", dict(depth=2)),
    "gabor_3d": ("gabor_texture", dict(type="2D")),
    "combine_hsv": ("combine_color", dict(color_type="rgb")),
    "vector_rotate_euler": ("vector_rotate", dict(rotate_type="axis")),
    "metallic_physical": ("metallic_bsdf", dict(fresnel_type="f82")),
    "hair_melanin": ("principled_hair_bsdf", dict(parametrization="Direct coloring")),
    "scatter_rayleigh": ("scatter_volume", dict(phase="Henyey-Greenstein")),
    "map_range_vector": ("vector_map_range", dict(range_type="linear")),
}
# Row by row from the top of the image (Blender +Y), left to right.
CELLS = (
    ("wave_saw", "brick_offset_squash", "clamp_range", "magic_depth"),
    ("gabor_3d", "mixrgb_use_alpha", "combine_hsv", "vector_rotate_euler"),
    ("metallic_physical", "mix_float", "hair_melanin", "hair_absorption"),
    ("scatter_rayleigh", "scatter_fournier_forand", "math_fract", "map_range_vector"),
)

CELLS_SCRIPT = (
    r"""
import sys
import bpy
import numpy as np

blend, image, size, samples = sys.argv[sys.argv.index("--") + 1 :][:4]
size, samples = int(size), int(samples)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.cycles_curves.shape = "THICK"
asset = bpy.data.collections.new("Cells")
scene.collection.children.link(asset)
COLUMNS, ROWS = (-2.7, -0.9, 0.9, 2.7), (2.7, 0.9, -0.9, -2.7)


def material(name):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.remove(nodes["Principled BSDF"])
    return result, nodes, links, nodes["Material Output"]


def coordinates(nodes):
    return nodes.new("ShaderNodeTexCoord").outputs["Object"]


def emit(nodes, links, out, color=None, strength=None):
    emission = nodes.new("ShaderNodeEmission")
    if color is not None:
        links.new(color, emission.inputs["Color"])
    if strength is not None:
        links.new(strength, emission.inputs["Strength"])
    links.new(emission.outputs[0], out.inputs["Surface"])


def x_ramp(nodes, links):
    # Object X of a 1.6 plane, mapped to 0..1.
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coordinates(nodes), split.inputs["Vector"])
    shift = nodes.new("ShaderNodeMath")
    shift.operation = "MULTIPLY_ADD"
    links.new(split.outputs["X"], shift.inputs[0])
    shift.inputs[1].default_value, shift.inputs[2].default_value = 0.625, 0.5
    return shift.outputs[0]


def place(obj, row, column, used):
    obj.location = (COLUMNS[column], ROWS[row], obj.location[2])
    obj.data.materials.append(used)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    return obj


def plane(name, row, column, used):
    bpy.ops.mesh.primitive_plane_add(size=1.6)
    place(bpy.context.active_object, row, column, used).name = name


def texture(name, kind, output, **settings):
    used, nodes, links, out = material(name)
    node = nodes.new(kind)
    for key, value in settings.items():
        if key.startswith("in_"):
            node.inputs[key[3:].replace("_", " ").title()].default_value = value
        else:
            setattr(node, key, value)
    links.new(coordinates(nodes), node.inputs["Vector"])
    emit(nodes, links, out, color=node.outputs[output] if output == "Color" else None,
         strength=node.outputs[output] if output != "Color" else None)
    return used


plane("wave_saw", 0, 0, texture("wave_saw", "ShaderNodeTexWave", "Color", wave_profile="SAW", in_scale=1.5))
plane("brick_offset_squash", 0, 1, texture("brick_offset_squash", "ShaderNodeTexBrick", "Color", offset=0.25,
      offset_frequency=3, squash=0.5, squash_frequency=3, in_scale=3.0))
used, nodes, links, out = material("clamp_range")
clamp = nodes.new("ShaderNodeClamp")
clamp.clamp_type = "RANGE"
clamp.inputs["Min"].default_value, clamp.inputs["Max"].default_value = 0.8, 0.2
links.new(x_ramp(nodes, links), clamp.inputs["Value"])
emit(nodes, links, out, strength=clamp.outputs[0])
plane("clamp_range", 0, 2, used)
plane("magic_depth", 0, 3, texture("magic_depth", "ShaderNodeTexMagic", "Color", turbulence_depth=5, in_scale=2.0))
plane("gabor_3d", 1, 0, texture("gabor_3d", "ShaderNodeTexGabor", "Intensity", gabor_type="3D", in_scale=3.0))
used, nodes, links, out = material("mixrgb_use_alpha")
mix = nodes.new("ShaderNodeMixRGB")
mix.use_alpha = True
mix.inputs["Fac"].default_value = 1.0
mix.inputs["Color1"].default_value, mix.inputs["Color2"].default_value = (1, 0, 0, 1), (0, 0, 1, 0.2)
emit(nodes, links, out, color=mix.outputs[0])
plane("mixrgb_use_alpha", 1, 1, used)
used, nodes, links, out = material("combine_hsv")
combine = nodes.new("ShaderNodeCombineColor")
combine.mode = "HSV"
links.new(x_ramp(nodes, links), combine.inputs[0])
combine.inputs[1].default_value = combine.inputs[2].default_value = 1.0
emit(nodes, links, out, color=combine.outputs[0])
plane("combine_hsv", 1, 2, used)
used, nodes, links, out = material("vector_rotate_euler")
rotate = nodes.new("ShaderNodeVectorRotate")
rotate.rotation_type = "EULER_XYZ"
rotate.inputs["Rotation"].default_value = (0.0, 0.0, 0.7)
links.new(coordinates(nodes), rotate.inputs["Vector"])
checker = nodes.new("ShaderNodeTexChecker")
checker.inputs["Scale"].default_value = 2.0
links.new(rotate.outputs[0], checker.inputs["Vector"])
emit(nodes, links, out, color=checker.outputs["Color"])
plane("vector_rotate_euler", 1, 3, used)
used, nodes, links, out = material("metallic_physical")
metal = nodes.new("ShaderNodeBsdfMetallic")
metal.fresnel_type = "PHYSICAL_CONDUCTOR"
metal.inputs["Roughness"].default_value = 0.35
metal.inputs["IOR"].default_value = (0.27, 0.68, 1.22)  # copper-like, unlike the grey F82 Base Color
metal.inputs["Extinction"].default_value = (3.6, 2.6, 2.3)
links.new(metal.outputs[0], out.inputs["Surface"])
plane("metallic_physical", 2, 0, used)
used, nodes, links, out = material("mix_float")
mix = nodes.new("ShaderNodeMix")
mix.data_type = "FLOAT"
links.new(x_ramp(nodes, links), mix.inputs["Factor"])
mix.inputs["A"].default_value, mix.inputs["B"].default_value = 0.2, 1.5
emit(nodes, links, out, strength=mix.outputs["Result"])
plane("mix_float", 2, 1, used)


def hair(name, row, column, parametrization):
    used, nodes, links, out = material(name)
    bsdf = nodes.new("ShaderNodeBsdfHairPrincipled")
    bsdf.parametrization = parametrization
    if parametrization == "MELANIN":  # lookups by name skip unavailable sockets
        bsdf.inputs["Melanin"].default_value = 0.3
    links.new(bsdf.outputs[0], out.inputs["Surface"])
    curves = bpy.data.hair_curves.new(name + "Data")
    curves.add_curves([4] * 12)
    points = [(-0.75 + 1.5 * i / 3, -0.66 + 0.12 * k, 0.0) for k in range(12) for i in range(4)]
    curves.attributes["position"].data.foreach_set("vector", np.array(points, np.float32).ravel())
    curves.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", np.full(48, 0.05, np.float32))
    obj = bpy.data.objects.new(name, curves)
    scene.collection.objects.link(obj)
    place(obj, row, column, used)


hair("hair_melanin", 2, 2, "MELANIN")
hair("hair_absorption", 2, 3, "ABSORPTION")


def scatter(name, column, phase):
    used, nodes, links, out = material(name)
    volume = nodes.new("ShaderNodeVolumeScatter")
    volume.phase = phase
    volume.inputs["Density"].default_value = 0.4  # mostly single scattering: the phase shows
    links.new(volume.outputs[0], out.inputs["Volume"])
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.4))
    box = bpy.context.active_object
    box.scale = (1.6, 1.6, 0.6)
    place(box, 3, column, used).name = name


scatter("scatter_rayleigh", 0, "RAYLEIGH")
scatter("scatter_fournier_forand", 1, "FOURNIER_FORAND")
used, nodes, links, out = material("math_fract")
scale = nodes.new("ShaderNodeMath")
scale.operation = "MULTIPLY"
links.new(x_ramp(nodes, links), scale.inputs[0])
scale.inputs[1].default_value = 3.0
fract = nodes.new("ShaderNodeMath")
fract.operation = "FRACT"
links.new(scale.outputs[0], fract.inputs[0])
emit(nodes, links, out, strength=fract.outputs[0])
plane("math_fract", 3, 2, used)
used, nodes, links, out = material("map_range_vector")
remap = nodes.new("ShaderNodeMapRange")
remap.data_type, remap.interpolation_type = "FLOAT_VECTOR", "SMOOTHSTEP"
links.new(coordinates(nodes), remap.inputs["Vector"])
vector_inputs = {s.identifier: s for s in remap.inputs}
vector_inputs["From_Min_FLOAT3"].default_value = (-0.8, -0.8, -0.2)  # Z (0 on a plane) at 0.2, not 0.5
vector_inputs["From_Max_FLOAT3"].default_value = (0.8, 0.8, 0.8)
emit(nodes, links, out, color=remap.outputs["Vector"])
plane("map_range_vector", 3, 3, used)

# The light: a large emitter above the camera (the camera looks down and does not see it).
used, nodes, links, out = material("overhead")
emission = nodes.new("ShaderNodeEmission")
emission.inputs["Strength"].default_value = 2.0
links.new(emission.outputs[0], out.inputs["Surface"])
bpy.ops.mesh.primitive_plane_add(size=40.0, location=(0.0, 0.0, 12.0))
overhead = bpy.context.active_object
overhead.data.materials.append(used)
for collection in list(overhead.users_collection):
    collection.objects.unlink(overhead)
asset.objects.link(overhead)
overhead.name = "overhead"

camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
camera.location = (0.0, 0.0, 10.0)
camera.data.lens, camera.data.sensor_width, camera.data.sensor_fit = 50.0, 36.0, "AUTO"
scene.collection.objects.link(camera)
scene.camera = camera
world = bpy.data.worlds.new("Black")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0, 0, 0, 1)
scene.world = world
bpy.ops.wm.save_as_mainfile(filepath=blend)
"""
    + r"""
render = scene.render
render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = samples
scene.cycles.use_denoising = False
scene.cycles.use_adaptive_sampling = False
render.resolution_x = render.resolution_y = size
render.resolution_percentage = 100
render.image_settings.file_format = "OPEN_EXR"
render.image_settings.color_depth = "32"
render.filepath = image + ".exr"
bpy.ops.render.render(write_still=True)
loaded = bpy.data.images.load(image + ".exr")
pixels = np.array(loaded.pixels[:], np.float32).reshape(size, size, 4)[::-1, :, :3]  # top row first
np.save(image + ".npy", pixels)
print("BLENDER_RENDER_DONE")
"""
)

SKY_SCRIPT = r"""
import math
import sys
import bpy
import numpy as np

blend, image, size, samples, elevation, rotation, sun_size = sys.argv[sys.argv.index("--") + 1 :][:7]
size, samples = int(size), int(samples)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Sky")
scene.collection.children.link(asset)
grey = bpy.data.materials.new("Grey")
grey.use_nodes = True
grey.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 1.0


def add(obj):
    obj.data.materials.append(grey)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    return obj


bpy.ops.mesh.primitive_plane_add(size=8.0)
add(bpy.context.active_object).name = "Floor"
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(-0.8, 0.6, 0.9))
box = add(bpy.context.active_object)
box.scale, box.name = (0.8, 0.8, 1.8), "Post"
camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
camera.location = (0.0, 0.0, 10.0)
camera.data.lens, camera.data.sensor_width, camera.data.sensor_fit = 50.0, 36.0, "AUTO"
scene.collection.objects.link(camera)
scene.camera = camera
world = bpy.data.worlds.new("Sky")
world.use_nodes = True
sky = world.node_tree.nodes.new("ShaderNodeTexSky")
sky.sun_elevation, sky.sun_rotation = math.radians(float(elevation)), math.radians(float(rotation))
sky.sun_size = math.radians(float(sun_size))
world.node_tree.links.new(sky.outputs["Color"], world.node_tree.nodes["Background"].inputs["Color"])
scene.world = world
bpy.ops.wm.save_as_mainfile(filepath=blend)
""" + CELLS_SCRIPT[CELLS_SCRIPT.index("\nrender = scene.render") :]


# Normals: row by row from the top; "-" is an empty region (black, compared too).
NORMAL_CELLS = (
    ("fresnel_normal_map", "fresnel_bump", "layer_fresnel_normal_map", "layer_facing_bump"),
    ("layer_facing_normal_map", "layer_fresnel_bump", "fresnel_unlinked", "layer_facing_unlinked"),
    ("-", "-", "-", "-"),
    ("-", "-", "-", "-"),
)
NORMALS_SCRIPT = (
    CELLS_SCRIPT[: CELLS_SCRIPT.index('\nplane("wave_saw"')]
    + r"""


def normal_cell(name, row, column, kind, output, normal):
    used, nodes, links, out = material(name)
    node = nodes.new(kind)
    if kind == "ShaderNodeFresnel":
        node.inputs["IOR"].default_value = 1.45
    else:
        node.inputs["Blend"].default_value = 0.5
    if normal == "normal_map":
        tilt = nodes.new("ShaderNodeNormalMap")
        tilt.inputs["Color"].default_value = (0.95, 0.5, 0.6, 1.0)  # tangent normal (0.9, 0, 0.2)
        links.new(tilt.outputs["Normal"], node.inputs["Normal"])
    elif normal == "bump":
        wave = nodes.new("ShaderNodeTexWave")
        wave.inputs["Scale"].default_value = 1.5
        links.new(coordinates(nodes), wave.inputs["Vector"])
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 1.0
        bump.inputs["Distance"].default_value = 0.2
        links.new(wave.outputs["Fac"], bump.inputs["Height"])
        links.new(bump.outputs["Normal"], node.inputs["Normal"])
    emit(nodes, links, out, strength=node.outputs[output])
    plane(name, row, column, used)


for row, cells in enumerate((
    (("fresnel_normal_map", "ShaderNodeFresnel", "Fac", "normal_map"),
     ("fresnel_bump", "ShaderNodeFresnel", "Fac", "bump"),
     ("layer_fresnel_normal_map", "ShaderNodeLayerWeight", "Fresnel", "normal_map"),
     ("layer_facing_bump", "ShaderNodeLayerWeight", "Facing", "bump")),
    (("layer_facing_normal_map", "ShaderNodeLayerWeight", "Facing", "normal_map"),
     ("layer_fresnel_bump", "ShaderNodeLayerWeight", "Fresnel", "bump"),
     ("fresnel_unlinked", "ShaderNodeFresnel", "Fac", None),
     ("layer_facing_unlinked", "ShaderNodeLayerWeight", "Facing", None)),
)):
    for column, cell in enumerate(cells):
        normal_cell(cell[0], row, column, *cell[1:])
"""
    + CELLS_SCRIPT[CELLS_SCRIPT.index("\n# The light:") :]
)


def errors(reference, ours, names=None):
    """Region (i, j) -> (relative error, Blender region mean); see the module docstring."""
    import numpy as np

    rows = np.array_split(np.arange(parity.SIZE), 4)
    result = {}
    for i, r in enumerate(rows):
        for j, c in enumerate(rows):
            a, b = reference[np.ix_(r, c)], ours[np.ix_(r, c)]
            scale = max(float(a.mean()), 0.05)
            noisy = names is None or names[i][j] in NOISY
            if noisy:
                error = float(np.abs(a.mean((0, 1)) - b.mean((0, 1))).max())
            else:
                error = float(np.abs(a - b).mean())
            result[(i, j)] = (error / scale if error > 0.002 else 0.0, round(float(a.mean()), 4))
    return result


def render(args, settings=None):
    """parity.render_scene, then remove the camera, world light and ROP it made."""
    before = {n.path() for root in ("/obj", "/out") for n in hou.node(root).children()}
    try:
        return parity.render_scene(args, samples=SAMPLES, settings=settings)
    finally:
        for root in ("/out", "/obj"):
            for node in hou.node(root).children():
                if node.path() not in before:
                    node.destroy()


def forget_render_nodes():
    """Remove the camera, black world and ROP parity.houdini_render leaves, so that a control
    render (render()) sees the same scene: with two black-world lights the world turns grey."""
    for root in ("/obj", "/out"):
        for node in hou.node(root).children():
            if node.name().startswith(("parity_cam", "parity_black_world", "parity_rop")):
                node.destroy()


def set_unlocked(node, values):
    """Set parameters of an imported (linked, locked) node for a control render."""
    for name, value in values.items():
        parm = node.parm(name)
        locked = parm.isLocked()
        parm.lock(False)
        parm.set(value)
        parm.lock(locked)


def native_nodes(kind):
    return list(hou.nodeType(hou.vopNodeTypeCategory(), "cycles::" + kind + "::1").instances())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--blender", default=None)
    parser.add_argument("--threshold", type=float, default=0.1, help="relative tolerance of region means")
    parser.add_argument("--points-performance", type=int, default=0)  # parity.blender_render reads args
    parser.add_argument("--scenes", default="cells,sky,normals", help="comma-separated scenes to run")
    args = parser.parse_args()
    scenes = set(args.scenes.split(","))
    check = parity.Checks()
    work = (args.work or Path(tempfile.mkdtemp(prefix="cycles_blend_node_settings_"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["CYCLES_HOUDINI_ASSET_CACHE"] = str(work / "cache")
    hou.putenv("CYCLES_HOUDINI_ASSET_CACHE", str(work / "cache"))
    print("Work folder:", work)
    import numpy as np

    def compare(label, reference, ours, names=None):
        check(f"{label}: Blender image is lit", float(reference.mean()) > 0.01, float(reference.mean()))
        found = errors(reference, ours, names)
        print(label + " ERRORS:" + json.dumps({f"{i},{j}": v for (i, j), v in found.items()}))
        for (i, j), (error, mean) in found.items():
            name = names[i][j] if names else f"{i},{j}"
            check(f"{label} {name}: within {args.threshold:.0%} of Blender", error <= args.threshold, (error, mean))
        return found

    def control(label, reference, names, cell, region, setting):
        """Put one imported setting back, re-render: that region must now miss Blender."""
        kind, values = setting
        nodes = native_nodes(kind)
        saved = [(node, {k: node.parm(k).eval() for k in values}) for node in nodes]
        try:
            for node in nodes:
                set_unlocked(node, values)
            ours = render(args, settings)
        finally:
            for node, old in saved:
                set_unlocked(node, old)
        error = errors(reference, ours, names)[region][0]
        check(f"{label} control {cell}: the reverted setting misses Blender", error > args.threshold, error)

    (work / "scene").mkdir(exist_ok=True)
    # 1. Cells
    if "cells" in scenes:
        executable, blend, reference = parity.blender_render(
            args, work, text=CELLS_SCRIPT, stem="cells", extra=(SAMPLES,)
        )
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "cells.hip"))
        _, ours = parity.houdini_render(args, executable, blend, collection="Cells", samples=SAMPLES)
        forget_render_nodes()
        np.save(work / "cells_houdini.npy", ours)
        parity.save_ppm(work / "cells_blender.ppm", reference)
        parity.save_ppm(work / "cells_houdini.ppm", ours)
        diagnostics = [d for d in parity.LAST_STATUS.get("source_diagnostics", ()) if d.get("severity") == "error"]
        check("Cells: no material error diagnostics", not diagnostics, diagnostics)
        compare("Cells", reference, ours, CELLS)
        settings = None
        for i, row in enumerate(CELLS):
            for j, cell in enumerate(row):
                if cell in CONTROLS:
                    control("Cells", reference, CELLS, cell, (i, j), CONTROLS[cell])

    # 2. Sky
    if "sky" in scenes:
        from cycles_houdini.blender import blend_worlds

        sky_args = (SAMPLES, SKY["sun_elevation"], SKY["sun_rotation"], SKY["sun_size"])
        executable, blend, reference = parity.blender_render(args, work, text=SKY_SCRIPT, stem="sky", extra=sky_args)
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "sky.hip"))
        from cycles_houdini.blender import blend_import_dialog as dialog
        import threading

        options = dict(mode="link", save_location="files", structure="packed", materials="link", frames="current")
        source, chosen, kwargs = dialog.converter_arguments(
            parity.posix(blend), [("collection", "Sky")], options, executable
        )
        backend = dialog.HoudiniBackend()
        nodes = backend.build(backend.prepare(source, chosen, kwargs, threading.Event(), lambda *a, **k: None))
        parity.drain()
        nodes[0].node("blender_file").cook(force=True)
        prepared = blend_worlds.prepare_world(blend, "Sky", root=work / "assets_blender", executable=executable)
        check("Sky: the world imports as a World Material", prepared["route"] == "material", prepared["route"])
        world = blend_worlds.build_world(prepared, mode="link", existing_environment="replace")["node"]
        skies = [n for n in world.allSubChildren() if n.type().name() == "cycles::sky_texture::1"]
        angles = {name: skies[0].evalParm(name) for name in SKY} if len(skies) == 1 else None
        check(
            "Sky: the Sky node shows Blender's angles in degrees",
            angles is not None and all(abs(angles[k] - v) < 1e-3 for k, v in SKY.items()),
            angles,
        )
        settings = dict(world_source="material", world_material=world.path())
        ours = render(args, settings)
        np.save(work / "sky_houdini.npy", ours)
        parity.save_ppm(work / "sky_blender.ppm", reference)
        parity.save_ppm(work / "sky_houdini.ppm", ours)
        compare("Sky", reference, ours)
        # Control: the angles as the import wrote them before (radians in the degree parameters).
        radians = {k: math.radians(v) for k, v in SKY.items()}
        set_unlocked(skies[0], radians)
        try:
            ours = render(args, settings)
        finally:
            set_unlocked(skies[0], SKY)
        worst = max(error for error, _ in errors(reference, ours).values())
        check("Sky control: radians in the degree parameters miss Blender", worst > args.threshold, worst)

    # 3. Normals
    if "normals" in scenes:
        executable, blend, reference = parity.blender_render(
            args, work, text=NORMALS_SCRIPT, stem="normals", extra=(SAMPLES,)
        )
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "normals.hip"))
        _, ours = parity.houdini_render(args, executable, blend, collection="Cells", samples=SAMPLES)
        forget_render_nodes()
        np.save(work / "normals_houdini.npy", ours)
        parity.save_ppm(work / "normals_blender.ppm", reference)
        parity.save_ppm(work / "normals_houdini.ppm", ours)
        diagnostics = [d for d in parity.LAST_STATUS.get("source_diagnostics", ()) if d.get("severity") == "error"]
        check("Normals: no material error diagnostics", not diagnostics, diagnostics)
        compare("Normals", reference, ours, NORMAL_CELLS)
        # Control: the Normal wires taken out of the imported Fresnel and Layer Weight nodes.
        wired = [
            (node, node.input(node.inputIndex("normal")), node.inputConnectors()[node.inputIndex("normal")])
            for kind in ("fresnel", "layer_weight")
            for node in native_nodes(kind)
            if node.input(node.inputIndex("normal")) is not None
        ]
        check("Normals: the import wires Normal on six native nodes", len(wired) == 6, len(wired))
        saved = [(node, node.inputIndex("normal"), connector[0]) for node, _, connector in wired]
        try:
            for node, index, _ in saved:
                node.setInput(index, None)
            ours = render(args, None)
            np.save(work / "normals_control_houdini.npy", ours)
        finally:
            for node, index, connection in saved:
                node.setInput(index, connection.inputNode(), connection.outputIndex())
        found = errors(reference, ours, NORMAL_CELLS)
        for i, row in enumerate(NORMAL_CELLS):
            for j, cell in enumerate(row):
                if cell != "-" and not cell.endswith("_unlinked"):
                    check(
                        f"Normals control {cell}: without the Normal wire it misses Blender",
                        found[(i, j)][0] > args.threshold,
                        found[(i, j)][0],
                    )
                elif cell.endswith("_unlinked"):
                    check(f"Normals control {cell}: unchanged", found[(i, j)][0] <= args.threshold, found[(i, j)][0])

    failed = [item for item in check.items if not item["ok"]]
    summary = dict(
        work=parity.posix(work), passed=len(check.items) - len(failed), failed=len(failed), checks=check.items
    )
    text = json.dumps(summary, indent=1, default=str)
    print("SUMMARY:" + text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
        code = 1
    try:
        from cycles_houdini.blender import blend_worker

        blend_worker.shutdown_all()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    sys.stdout.flush()
    hou.exit(code, suppress_save_prompt=True)
    os._exit(code)
