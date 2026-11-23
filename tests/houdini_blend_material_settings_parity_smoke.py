# SPDX-License-Identifier: Apache-2.0
"""Link-mode parity for Blender's per-material settings (Material > Settings).

    hython tests/houdini_blend_material_settings_parity_smoke.py [--work DIR] [--device CPU] [--blender EXE]

A generated .blend (collection "Settings"): a diffuse floor (Bump Map Correction off), a block of 400
small emissive cubes with Emission Sampling None that light the floor, and a cube with a Volume
Scatter medium around a small emissive sphere, whose Volume Sampling is Equiangular and
Interpolation Cubic. Blender 5.3's Cycles renders it
(the worker's executable); Houdini renders the collection imported in Link mode with the same camera.
Checks: the translated Cycles Materials carry the settings (parameters and the graph sent to the
renderer), and the two images agree region by region (both are noisy: None leaves the floor lit only
by paths that hit the emitters, so regions are compared by mean). Uses the helpers of
houdini_blend_sync_parity_smoke.py. Prints a JSON summary; exits non-zero when a check failed.
"""

import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import hou

sys.path.insert(0, str(Path(__file__).resolve().parent))
import houdini_blend_sync_parity_smoke as parity  # noqa: E402

SAMPLES = 64
BLEND_SCRIPT = r"""
import sys
import bpy
import numpy as np

blend, image, size, samples, fog_sampling, fog_interpolation = sys.argv[sys.argv.index("--") + 1 :][:6]
size, samples = int(size), int(samples)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Settings")
scene.collection.children.link(asset)


def material(name):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    return result, result.node_tree.nodes, result.node_tree.links


floor_material, nodes, links = material("Floor")
nodes["Principled BSDF"].inputs["Roughness"].default_value = 1.0
floor_material.cycles.use_bump_map_correction = False
field, nodes, links = material("Field")
nodes.remove(nodes["Principled BSDF"])
emission = nodes.new("ShaderNodeEmission")
emission.inputs["Color"].default_value = (1.0, 0.8, 0.6, 1.0)
emission.inputs["Strength"].default_value = 3.0
links.new(emission.outputs["Emission"], nodes["Material Output"].inputs["Surface"])
field.cycles.emission_sampling = "NONE"
fog, nodes, links = material("Fog")
nodes.remove(nodes["Principled BSDF"])
# Volume Scatter reads no attribute. (A mesh medium that reads a missing attribute, such as
# Principled Volume's default "density", renders as Blender's Cycles renders it since task L1:
# tests/houdini_blend_sync_parity_smoke.py, asset "Media".)
volume = nodes.new("ShaderNodeVolumeScatter")
volume.inputs["Density"].default_value = 1.0
links.new(volume.outputs["Volume"], nodes["Material Output"].inputs["Volume"])
fog.cycles.volume_sampling = fog_sampling
fog.cycles.volume_interpolation = fog_interpolation


def add(obj, used):
    obj.data.materials.append(used)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    return obj


bpy.ops.mesh.primitive_plane_add(size=8.0, location=(0.0, 0.0, 0.0))
add(bpy.context.active_object, floor_material).name = "Floor"
import bmesh

mesh = bpy.data.meshes.new("Field")
built = bmesh.new()
for i in range(20):
    for j in range(20):
        cube = bmesh.ops.create_cube(built, size=0.06)
        bmesh.ops.translate(built, verts=cube["verts"], vec=(-2.85 + 0.3 * i, -2.85 + 0.3 * j, 1.2))
built.to_mesh(mesh)
built.free()
add(bpy.data.objects.new("Field", mesh), field)
bpy.ops.mesh.primitive_cube_add(size=1.2, location=(2.2, 2.2, 0.8))
add(bpy.context.active_object, fog).name = "Fog"
# A sampled light (Emission Sampling Auto) inside the medium, so Equiangular sampling has work.
lamp, nodes, links = material("Lamp")
nodes.remove(nodes["Principled BSDF"])
glow = nodes.new("ShaderNodeEmission")
glow.inputs["Strength"].default_value = 20.0
links.new(glow.outputs["Emission"], nodes["Material Output"].inputs["Surface"])
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.08, location=(2.2, 2.2, 0.8))
add(bpy.context.active_object, lamp).name = "Lamp"

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


def builders():
    kind = hou.nodeType(hou.vopNodeTypeCategory(), "cycles::material::1")
    return {node.name(): node for node in kind.instances()}


def main():
    import numpy as np

    from cycles_houdini import materials

    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--blender", default=None)
    parser.add_argument("--threshold", type=float, default=0.1, help="relative tolerance of region means")
    parser.add_argument("--points-performance", type=int, default=0)  # parity.blender_render reads args
    parser.add_argument("--fog-sampling", default="EQUIANGULAR", help="control runs: MULTIPLE_IMPORTANCE")
    parser.add_argument("--fog-interpolation", default="CUBIC")
    parser.add_argument("--rop-settings", type=json.loads, default=None, help="JSON of ROP parameter values")
    args = parser.parse_args()
    check = parity.Checks()
    work = (args.work or Path(tempfile.mkdtemp(prefix="cycles_blend_material_settings_"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["CYCLES_HOUDINI_ASSET_CACHE"] = str(work / "cache")
    hou.putenv("CYCLES_HOUDINI_ASSET_CACHE", str(work / "cache"))
    print("Work folder:", work)
    executable, blend, reference = parity.blender_render(
        args, work, text=BLEND_SCRIPT, stem="settings", extra=(SAMPLES, args.fog_sampling, args.fog_interpolation)
    )
    hou.hipFile.clear(suppress_save_prompt=True)
    (work / "scene").mkdir(exist_ok=True)
    hou.hipFile.save(str(work / "scene" / "settings.hip"))
    node, ours = parity.houdini_render(
        args, executable, blend, collection="Settings", samples=SAMPLES, settings=args.rop_settings
    )
    np.save(work / "houdini.npy", ours)
    parity.save_ppm(work / "blender.ppm", reference)
    parity.save_ppm(work / "houdini.ppm", ours)
    found = builders()
    print("BUILDERS:" + json.dumps(sorted(found)))

    def builder(label):
        named = [b for name, b in found.items() if label.lower() in name.lower()]
        return named[0] if len(named) == 1 else None

    expected = dict(
        Field=(dict(cycles_emission_sampling="none"), dict(emission_sampling="none")),
        Fog=(
            dict(
                cycles_volume_sampling=args.fog_sampling.lower(),
                cycles_volume_interpolation=args.fog_interpolation.lower(),
            ),
            {
                key: value.lower()
                for key, value, default in (
                    ("volume_sampling", args.fog_sampling, "MULTIPLE_IMPORTANCE"),
                    ("volume_interpolation", args.fog_interpolation, "LINEAR"),
                )
                if value != default
            }
            or None,
        ),
        Floor=(dict(cycles_bump_map_correction=0), dict(bump_map_correction=False)),
    )
    for label, (parms, settings) in expected.items():
        target = builder(label)
        if not check(f"{label}: one translated Cycles Material", target is not None, sorted(found)):
            continue
        actual = {name: target.evalParm(name) for name in parms}
        check(f"{label}: Blender settings on the material parameters", actual == parms, actual)
        graph = materials.material_graph(target).get("settings")
        check(f"{label}: settings in the graph sent to the renderer", graph == settings, graph)
    rows = np.array_split(np.arange(parity.SIZE), 4)
    regions = {}
    for i, r in enumerate(rows):
        for j, c in enumerate(rows):
            a, b = float(reference[np.ix_(r, c)].mean()), float(ours[np.ix_(r, c)].mean())
            regions[f"{i},{j}"] = (round(a, 4), round(b, 4))
    print("REGIONS:" + json.dumps(regions))
    means = (float(reference.mean()), float(ours.mean()))
    check("Blender image is lit", means[0] > 0.01, means)
    check(f"image means within {args.threshold:.0%}", abs(means[1] / means[0] - 1) < args.threshold, means)
    bad = {k: v for k, v in regions.items() if abs(v[1] - v[0]) > args.threshold * max(v[0], 0.05)}
    check(f"all 16 region means within {args.threshold:.0%} (or 0.005 in dark regions)", not bad, bad)
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
