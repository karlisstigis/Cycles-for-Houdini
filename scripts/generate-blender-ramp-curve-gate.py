# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the bounded K33 reference using pinned Blender's actual Cycles renders."""

import bpy, json, struct, sys
from pathlib import Path
import numpy as np

assert bpy.app.build_hash.decode() == "fbe6228777e7", "Use the pinned Blender reference build"
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "tests"))
from support.package_loader import import_blender

export_materials = import_blender("blend_material_export").export_materials
out = root / "artifacts/ramp-curve-k33"
out.mkdir(parents=True, exist_ok=True)
bits = lambda f: struct.unpack("i", struct.pack("f", f))[0]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 1
scene.cycles.use_denoising = False
preferences = bpy.context.preferences.addons["cycles"].preferences
preferences.compute_device_type = "OPTIX"
preferences.get_devices()
for device in preferences.devices:
    device.use = device.type == "OPTIX" and "4090" in device.name
assert any(device.use for device in preferences.devices), "Pinned reference needs the matching RTX 4090 OptiX device"
scene.render.resolution_x, scene.render.resolution_y = 192, 144
scene.render.resolution_percentage = 100
bpy.ops.object.camera_add(location=(0, 0, 12))
scene.camera = bpy.context.object
scene.camera.data.type = "ORTHO"
scene.camera.data.ortho_scale = 12
material = bpy.data.materials.new("K33 independent reference")
material.use_nodes = True
graph = material.node_tree
graph.nodes.clear()
info = graph.nodes.new("ShaderNodeObjectInfo")
sep = graph.nodes.new("ShaderNodeSeparateXYZ")
emit = graph.nodes.new("ShaderNodeEmission")
end = graph.nodes.new("ShaderNodeOutputMaterial")
graph.links.new(info.outputs["Color"], sep.inputs[0])
graph.links.new(emit.outputs[0], end.inputs["Surface"])
objects = []
for i in range(4):
    bpy.ops.mesh.primitive_plane_add(size=1.8, location=(-4.5 + i * 3, 0, 0))
    obj = bpy.context.object
    obj.data.materials.append(material)
    objects.append(obj)

cases = []
specs = [("ridge_aligned", "ramp", 0.5), ("ridge_off_grid", "ramp", 0.5007), ("constant_subsample", "constant", 0.5007)]
specs += [("hue_" + direction.lower(), direction, 0.5) for direction in ("NEAR", "FAR", "CW", "CCW")]
specs += [(kind + "_ridge", kind, 0.5) for kind in ("float_curve", "rgb_curves", "vector_curves")]
specs += [("rgb_curves_nonlinear_combined", "rgb_curves", 0.425)]
specs += [
    (space.lower() + "_constant_" + output, space + "_CONSTANT", 0.5)
    for space in ("HSV", "HSL")
    for output in ("color", "alpha")
]
for name, kind, peak in specs:
    factors = list(
        np.asarray(
            [
                np.nextafter(np.float32(peak), np.float32(0)),
                peak,
                np.nextafter(np.float32(peak), np.float32(1)),
                peak - 0.0005,
            ],
            np.float32,
        ).astype(float)
    )
    if kind in ("NEAR", "FAR", "CW", "CCW"):
        factors = list(np.asarray([0.4997, 0.5001, 0.5004, 0.5008], np.float32).astype(float))
    keys, values = [0, peak - 0.001, peak, peak + 0.001, 1], [0, 0, 1, 0, 0]
    nonlinear = name == "rgb_curves_nonlinear_combined"
    if nonlinear:
        keys, values = [0, 0.23, 0.68, 1], [0, 0.83, 0.12, 1]
        factors = list(np.asarray([0.13377, 0.27123, 0.49731, 0.91234], np.float32).astype(float))
    hue_constant = kind in ("HSV_CONSTANT", "HSL_CONSTANT")
    alpha_output = name.endswith("_alpha")
    if kind in ("ramp", "constant", "NEAR", "FAR", "CW", "CCW") or hue_constant:
        node = graph.nodes.new("ShaderNodeValToRGB")
        ramp = node.color_ramp
        while len(ramp.elements) > 1:
            ramp.elements.remove(ramp.elements[-1])
        colors = [(v, v, v, 1) for v in values]
        if kind == "constant":
            keys = [0, peak, peak + 0.00001, 1]
            colors = [(0, 0, 0, 1), (1, 1, 1, 1), (0, 0, 0, 1), (0, 0, 0, 1)]
            ramp.interpolation = "CONSTANT"
        else:
            ramp.interpolation = "LINEAR"
        hue = kind in ("NEAR", "FAR", "CW", "CCW") or hue_constant
        if hue:
            keys = [0, 0.499, 0.501, 1]
            colors = [(1, 0, 0.3, 0.2), (1, 0, 0.3, 0.8), (1, 0.3, 0, 0.4), (1, 0.3, 0, 0.9)]
            ramp.color_mode, ramp.hue_interpolation = (
                kind[:3] if hue_constant else "HSV",
                "FAR" if hue_constant else kind,
            )
            if hue_constant:
                ramp.interpolation = "CONSTANT"
                factors = list(np.asarray([0.4997, 0.5001, 0.5004, 0.5008], np.float32).astype(float))
        ramp.elements[0].position = keys[0]
        for key in keys[1:]:
            ramp.elements.new(key)
        for element, color in zip(ramp.elements, colors):
            element.color = color
        source = [
            0x43525331,
            2 if hue else 5,
            len(keys),
            3 if hue_constant and kind[:3] == "HSL" else int(hue),
            1,
            (1 if hue_constant else ("NEAR", "FAR", "CW", "CCW").index(kind)) if hue else int(kind != "constant"),
            0,
            0,
        ]
        for e in ramp.elements:
            source += [1, bits(e.position), *map(bits, e.color[:3])]
        table = [
            list(ramp.evaluate(i / 256))[:3] if not alpha_output else [ramp.evaluate(i / 256)[3]] * 3
            for i in range(257)
        ]
        authored = [list(ramp.evaluate(x))[:3] if not alpha_output else [ramp.evaluate(x)[3]] * 3 for x in factors]
        graph.links.new(sep.outputs["X"], node.inputs[0])
        graph.links.new(node.outputs["Alpha" if alpha_output else "Color"], emit.inputs[0])
        interpolate = kind != "constant" and not hue_constant
        native_kind = "ramp"
    else:
        identifier = {
            "float_curve": "ShaderNodeFloatCurve",
            "rgb_curves": "ShaderNodeRGBCurve",
            "vector_curves": "ShaderNodeVectorCurve",
        }[kind]
        node = graph.nodes.new(identifier)
        mapping = node.mapping
        mapping.initialize()
        mapping.use_clip = False
        mapping.clip_min_x, mapping.clip_max_x = 0, 1
        mapping.extend = "HORIZONTAL"
        for index, curve in enumerate(mapping.curves):
            master = kind == "rgb_curves" and index == 3
            for p in list(curve.points)[1:-1]:
                curve.points.remove(p)
            for x, y in zip(keys[1:-1], values[1:-1]):
                if not master:
                    curve.points.new(x, y)
            curve.points[0].location = (0, 0)
            curve.points[-1].location = (1, 1 if master else 0)
            if nonlinear:
                if master:
                    curve.points.new(0.37, 0.72)
                curve.points[-1].location = (1, 1)
            for p in curve.points:
                p.handle_type = "AUTO_CLAMPED" if nonlinear else "VECTOR"
        mapping.update()
        curve_kind = ("float_curve", "rgb_curves", "vector_curves").index(kind)
        source = [0x43435231, 1, curve_kind, 0, bits(0), bits(1), 0, len(mapping.curves)]
        for curve in mapping.curves:
            source += [len(curve.points)]
            for p in curve.points:
                source += [bits(p.location.x), bits(p.location.y), 1 if nonlinear else 2]

        def evaluate(x):
            if kind == "float_curve":
                return [mapping.evaluate(mapping.curves[0], x)] * 3
            t = mapping.evaluate(mapping.curves[3], x) if kind == "rgb_curves" else x
            return [mapping.evaluate(mapping.curves[c], t) for c in range(3)]

        table, authored = [evaluate(i / 256) for i in range(257)], [evaluate(x) for x in factors]
        node.inputs["Factor" if kind == "float_curve" else "Fac"].default_value = 1
        graph.links.new(
            sep.outputs["X"] if kind == "float_curve" else info.outputs["Color"],
            node.inputs["Value" if kind == "float_curve" else "Color" if kind == "rgb_curves" else "Vector"],
        )
        graph.links.new(node.outputs[0], emit.inputs[0])
        interpolate, native_kind = True, kind
    for obj, factor in zip(objects, factors):
        obj.color = (factor, factor, factor, 1)
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.render.filepath = str(out / (name + "-blender.exr"))
    scene.cycles.device = "CPU"
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(scene.render.filepath, check_existing=False)
    pixels = np.asarray(image.pixels[:], np.float32).reshape(144, 192, 4)[72, [24, 72, 120, 168], :3]
    bpy.data.images.remove(image)
    scene.cycles.device = "GPU"
    scene.render.filepath = str(out / (name + "-blender-optix.exr"))
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(scene.render.filepath, check_existing=False)
    optix_pixels = np.asarray(image.pixels[:], np.float32).reshape(144, 192, 4)[72, [24, 72, 120, 168], :3]
    bpy.data.images.remove(image)
    # Keep a saved Blender project and the real exporter record. The Houdini
    # regression imports this material through the product importer.
    bpy.ops.wm.save_as_mainfile(filepath=str(out / (name + ".blend")))
    exported = export_materials([material], out)
    np.asarray(source, "<i4").tofile(out / (name + ".source"))
    np.asarray(factors, "<f4").tofile(out / (name + ".inputs"))
    cases.append(
        dict(
            id=name,
            kind=native_kind,
            factors=factors,
            authored=authored,
            blender_pixels=pixels.tolist(),
            blender_optix_pixels=optix_pixels.tolist(),
            material=exported["materials"][0],
        )
    )
    graph.nodes.remove(node)
record = dict(
    blender_version=bpy.app.version_string,
    blender_commit=bpy.app.build_hash.decode(),
    sampling=dict(
        samples=1,
        denoise=False,
        adaptive_sampling=False,
        working_space="Linear Rec.709",
        cpu="Blender CPU",
        optix=[d.name for d in preferences.devices if d.use],
    ),
    reference="Actual Cycles linear-RGB EXR pixels; independently authored public controls",
    cases=cases,
)
(root / "tests/fixtures/blender-ramp-curve-gate.json").write_text(json.dumps(record, separators=(",", ":")) + "\n")
print(json.dumps(dict(result="passed", cases=len(cases))))
