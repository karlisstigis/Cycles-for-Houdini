# SPDX-License-Identifier: Apache-2.0
"""Blender half of the world orientation gate (blend-import.md: Worlds).

Writes a marked equirectangular HDRI (orientation_marks.exr), a library .blend with one
simple world per Mapping Z rotation (Orient_000, Orient_090, Orient_215), and renders with
Cycles CPU: for every world, a probe camera at the origin looking where each mark must
appear, and a mirror sphere seen from Blender +Z (evidence images). Writes
blender_report.json: per angle and mark, the probe colour and whether it is that mark.

Run with Blender as a Python module, or inside Blender:
    python orientation_reference.py <output folder>
    blender -b --factory-startup --python orientation_reference.py -- <output folder>

Marks (directions in the image's own lookup coordinates, Blender world Z up):
    +X red, -X green, +Y blue, -Y yellow, zenith (+Z) white; elsewhere dark grey.
A Mapping (Point) rotation z looks the image up at R_z(z) d, so mark m shows in the
direction R_z(-z) m: the image turns opposite to the Mapping rotation.
"""

import json
import math
import sys
from pathlib import Path

ANGLES = (0, 90, 215)
MARKS = {
    "+X": ((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    "-X": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "+Y": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "-Y": ((0.0, -1.0, 0.0), (1.0, 1.0, 0.0)),
    "zenith": ((0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
}
BACKGROUND = (0.05, 0.05, 0.05)
RADIUS = math.radians(20.0)
WIDTH, HEIGHT = 256, 128


def world_name(angle):
    return f"Orient_{angle:03d}"


def mark_direction(mark, angle):
    """Where `mark` shows with Mapping Z rotation `angle` degrees: R_z(-angle) m (Blender world)."""
    x, y, z = MARKS[mark][0]
    c, s = math.cos(math.radians(-angle)), math.sin(math.radians(-angle))
    return (c * x - s * y, s * x + c * y, z)


def pixel_direction(row, column):
    """Lookup direction of a file pixel (row 0 = top = zenith; u = 0.5 is +X, u = 0.25 is +Y)."""
    u, v = (column + 0.5) / WIDTH, (row + 0.5) / HEIGHT
    phi, theta = (0.5 - u) * 2.0 * math.pi, v * math.pi
    return (math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta))


def marked_pixels():
    """Top-down rows of RGB tuples."""
    rows = []
    for row in range(HEIGHT):
        line = []
        for column in range(WIDTH):
            d = pixel_direction(row, column)
            colour = BACKGROUND
            for direction, rgb in MARKS.values():
                if math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(d, direction))))) < RADIUS:
                    colour = rgb
            line.append(colour)
        rows.append(line)
    return rows


def classify(rgb):
    """The mark (or "background") whose colour is nearest to rgb."""
    candidates = dict((name, colour) for name, (_, colour) in MARKS.items())
    candidates["background"] = BACKGROUND
    return min(candidates, key=lambda name: sum((a - b) ** 2 for a, b in zip(rgb, candidates[name])))


def main(out):
    import bpy
    from mathutils import Vector

    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    hdri = bpy.data.images.new("orientation_marks", WIDTH, HEIGHT, float_buffer=True)
    pixels = []
    for line in reversed(marked_pixels()):  # Blender stores rows bottom-up
        for rgb in line:
            pixels.extend((*rgb, 1.0))
    hdri.pixels[:] = pixels
    hdri.filepath_raw = str(out / "orientation_marks.exr")
    hdri.file_format = "OPEN_EXR"
    hdri.save()
    bpy.data.images.remove(hdri)
    image = bpy.data.images.load(str(out / "orientation_marks.exr"))
    image.colorspace_settings.name = "Non-Color"
    for angle in ANGLES:
        world = bpy.data.worlds.new(world_name(angle))
        world.use_fake_user = True
        tree = world.node_tree
        env = tree.nodes.new("ShaderNodeTexEnvironment")
        env.image = image
        mapping = tree.nodes.new("ShaderNodeMapping")
        mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(angle))
        coordinate = tree.nodes.new("ShaderNodeTexCoord")
        tree.links.new(coordinate.outputs["Generated"], mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
        tree.links.new(env.outputs["Color"], tree.nodes["Background"].inputs["Color"])
    bpy.ops.wm.save_as_mainfile(filepath=str(out / "orientation_worlds.blend"))

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.view_settings.view_transform = "Standard"
    scene.render.image_settings.file_format = "OPEN_EXR"
    camera = bpy.data.objects.new("probe", bpy.data.cameras.new("probe"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    material = bpy.data.materials.new("mirror")
    shader = material.node_tree.nodes["Principled BSDF"]
    shader.inputs["Metallic"].default_value = 1.0
    shader.inputs["Roughness"].default_value = 0.0
    shader.inputs["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    report = dict(angles={}, ok=True)
    for angle in ANGLES:
        scene.world = bpy.data.worlds[world_name(angle)]
        probes = {}
        camera.data.type, camera.data.lens = "PERSP", 400.0
        scene.render.resolution_x = scene.render.resolution_y = 8
        camera.location = (0.0, 0.0, 0.0)
        for mark in MARKS:
            camera.rotation_euler = Vector(mark_direction(mark, angle)).to_track_quat("-Z", "Y").to_euler()
            scene.render.filepath = str(out / f"blender_{angle:03d}_{mark}.exr")
            bpy.ops.render.render(write_still=True)
            result = bpy.data.images.load(scene.render.filepath, check_existing=False)
            values = list(result.pixels)
            centre = ((4 * 8) + 4) * 4
            rgb = [round(v, 4) for v in values[centre : centre + 3]]
            bpy.data.images.remove(result)
            probes[mark] = dict(rgb=rgb, seen=classify(rgb), direction=list(mark_direction(mark, angle)))
            report["ok"] &= probes[mark]["seen"] == mark
        # Mirror sphere seen from Blender +Z (image up = Blender +Y), evidence only.
        sphere = bpy.data.meshes.new("sphere")
        import bmesh

        mesh = bmesh.new()
        bmesh.ops.create_uvsphere(mesh, u_segments=64, v_segments=32, radius=1.0)
        mesh.to_mesh(sphere)
        mesh.free()
        for polygon in sphere.polygons:
            polygon.use_smooth = True
        sphere.materials.append(material)
        ball = bpy.data.objects.new("mirror_ball", sphere)
        scene.collection.objects.link(ball)
        camera.data.type, camera.data.ortho_scale = "ORTHO", 2.2
        camera.location, camera.rotation_euler = (0.0, 0.0, 5.0), (0.0, 0.0, 0.0)
        scene.render.resolution_x = scene.render.resolution_y = 96
        scene.render.filepath = str(out / f"blender_{angle:03d}_mirror_ball.exr")
        bpy.ops.render.render(write_still=True)
        bpy.data.objects.remove(ball)
        bpy.data.meshes.remove(sphere)
        report["angles"][str(angle)] = probes
    (out / "blender_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("RESULT:" + json.dumps(report))
    return report


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    main(arguments[0])
