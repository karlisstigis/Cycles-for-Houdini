# SPDX-License-Identifier: Apache-2.0
"""Run inside headless Blender: evaluated topology, nested offsets, time and bundle fields.

blender --background --factory-startup --python-exit-code 1 --python tests/blender_asset_export_smoke.py -- --output <directory>
"""

import argparse
import bpy, json, math, sys
from support.package_loader import import_blender
from pathlib import Path
import numpy as np
from mathutils import Matrix, Vector

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
root = Path(__file__).resolve().parents[1]
qa = args.output.resolve()
qa.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
inner = bpy.data.collections.new("Inner")
inner.instance_offset = (0, 1, 0)
outer = bpy.data.collections.new("Outer")
outer.instance_offset = (1, 0, 0)
mesh = bpy.data.meshes.new("OriginalTopology")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0.2), (0, 1, 0)], [], [(0, 1, 2, 3)])
mesh.uv_layers.new(name="PaintUV")
for loop in mesh.uv_layers[0].data:
    loop.uv = (0.25, 0.75)
mesh.attributes.new("roughness", "FLOAT", "POINT")
mesh.color_attributes.new(name="Tint", type="FLOAT_COLOR", domain="CORNER")
for d in mesh.color_attributes["Tint"].data:
    d.color = (0.1, 0.2, 0.3, 1)
obj = bpy.data.objects.new("Selected Mesh", mesh)
inner.objects.link(obj)
obj.location = (1, 2, 3)
obj.keyframe_insert(data_path="location", frame=1)
obj.location.x = 2
obj.keyframe_insert(data_path="location", frame=2)
for fcurve in obj.animation_data.action.layers[0].strips[0].channelbag(obj.animation_data.action_slot).fcurves:
    for p in fcurve.keyframe_points:
        p.interpolation = "LINEAR"
mat = bpy.data.materials.new("Surface")
mat.use_nodes = True
mesh.materials.append(mat)
modifier = obj.modifiers.new("Render only thickness", "SOLIDIFY")
modifier.thickness = 0.1
modifier.show_viewport = False
modifier.show_render = True
nested = bpy.data.objects.new("Nested", None)
outer.objects.link(nested)
nested.location = (4, 0, 0)
nested.instance_type = "COLLECTION"
nested.instance_collection = inner
assembly = bpy.data.objects.new("Assembly", None)
scene.collection.objects.link(assembly)
assembly.location = (10, 0, 0)
assembly.instance_type = "COLLECTION"
assembly.instance_collection = outer
# No camera: prototype-only source libraries must also evaluate successfully.
source = qa / "nested.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(source))
p = root / "houdini/python3.13libs/cycles_houdini/blend_export.py"
module = import_blender("blend_export")
checks = []
for kind, name, expected in [
    ("OBJECT", "Assembly", (14.25, 3, -1)),
    ("COLLECTION", "Outer", (4.25, 3, -1)),
    ("OBJECT", "Selected Mesh", (1.25, 3, -2)),
]:
    output = qa / (kind + "-" + name.replace(" ", "_"))
    manifest = module.export(dict(source=str(source), selector_type=kind, selector=name, frame=1.25), output)
    assert manifest["depsgraph_mode"] == "RENDER", manifest["depsgraph_mode"]
    assert len(manifest["instances"]) == 1, [
        (r["name"], r["parent_name"], r["transform"][3::4]) for r in manifest["instances"]
    ]
    instance = manifest["instances"][0]
    actual = np.array(instance["transform"]).reshape(4, 4)[:3, 3]
    assert np.allclose(actual, expected, atol=1e-5), (kind, name, actual, expected)
    record = manifest["meshes"][0]
    with np.load(output / "geometry.npz", allow_pickle=False) as archive:
        assert archive[record["arrays"]["positions"]].shape[0] == 8, record
        assert record["default_uv"] == "PaintUV", record
        assert len(record["color_attributes"]) == 1, record
        assert archive[record["arrays"]["triangle_vertices"]].shape[0] == 12, record
        assert archive[record["arrays"]["triangle_loops"]].shape == (12, 3), record
    assert len(manifest["materials"]) == 1, manifest["materials"]
    assert str(source.resolve()) in manifest["dependencies"]
    assert manifest["color_management"]["color_interop_id"] == "lin_rec709_scene", manifest["color_management"]
    checks.append(
        dict(selector_type=kind, selector=name, translation=actual.tolist(), triangles=record["triangle_count"])
    )
# Children excluded in the source view layer (rig widgets) stay out by default.
# Members outside the view layer must still keep their own meshes.
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def triangle(name, x):
    data = bpy.data.meshes.new(name)
    data.from_pydata([(x, 0, 0), (x + 1, 0, 0), (x, 1, 0)], [], [(0, 1, 2)])
    return bpy.data.objects.new(name, data)


rig = bpy.data.collections.new("Rig Asset")
scene.collection.children.link(rig)
rig.instance_offset = (0.5, 0, 0)
rig.objects.link(triangle("Body", 0))
widgets = bpy.data.collections.new("wdg_widgets")
rig.children.link(widgets)
widgets.objects.link(triangle("WGT_root", 10))
loose = bpy.data.collections.new("Unlinked")
for index in range(4):
    loose.objects.link(triangle("Loose" + str(index), 10 * index))
loose.use_fake_user = True
bpy.context.view_layer.layer_collection.children["Rig Asset"].children["wdg_widgets"].exclude = True
source = qa / "excluded.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(source))
for selector, options, expected in [
    ("Rig Asset", {}, ["Body"]),
    ("Rig Asset", dict(include_excluded_collections=True), ["Body", "WGT_root"]),
    ("Unlinked", {}, ["Loose0", "Loose1", "Loose2", "Loose3"]),
]:
    request = dict(source=str(source), selector_type="COLLECTION", selector=selector, frame=1, **options)
    manifest, arrays = module.evaluate(request, qa / "excluded")
    names = sorted(instance["name"] for instance in manifest["instances"])
    assert names == expected, (selector, options, names)
    meshes = {mesh["key"]: mesh for mesh in manifest["meshes"]}
    origins = dict(Body=-0.5, WGT_root=9.5, Loose0=0, Loose1=10, Loose2=20, Loose3=30)
    for instance in manifest["instances"]:
        x = arrays[meshes[instance["mesh_key"]]["arrays"]["positions"]][:, 0].min() + instance["transform"][3]
        assert abs(x - origins[instance["name"]]) < 1e-5, (instance["name"], x)
    skipped = [issue["source"] for issue in manifest["diagnostics"] if issue["code"] == "blend.excluded_collection"]
    assert skipped == (["wdg_widgets"] if names == ["Body"] else []), skipped
    # Only the opted-in selection marks its manifest, so its bounds record
    # pairs with the path.blend#collection+disabled=Name reference.
    assert manifest.get("include_excluded_collections", False) is bool(options), manifest.keys()
    checks.append(dict(selector_type="COLLECTION", selector=selector, members=names, skipped=skipped))
(qa / "report.json").write_text(json.dumps(dict(result="passed", checks=checks), indent=2))
print("EXPORT_QA_PASSED", json.dumps(checks))
