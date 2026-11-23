# SPDX-License-Identifier: Apache-2.0
"""Run with Blender --background --python; independent owned material fixture."""

from support.paths import artifacts_root

from pathlib import Path
from support.package_loader import import_blender
import json
import sys
import bpy
import numpy as np

root = Path(__file__).resolve().parents[1]
destination = artifacts_root() / "blend-material-transfer"
destination.mkdir(parents=True, exist_ok=True)
module = import_blender("blend_material_export")
mat = bpy.data.materials.new("Owned textured group")
mat.use_nodes = True
nodes, links = mat.node_tree.nodes, mat.node_tree.links
bsdf = nodes.get("Principled BSDF")
bsdf.inputs["Roughness"].default_value = 0.31
image = bpy.data.images.new("Packed color", width=2, height=2)
image.pixels = [1.0, 0.2, 0.05, 1.0] * 4
image.filepath_raw = str(destination / "original.png")
image.file_format = "PNG"
image.save()
image.pack()
image.source = "FILE"
texture = nodes.new("ShaderNodeTexImage")
texture.image = image
uv = nodes.new("ShaderNodeUVMap")
uv.uv_map = "UVMap"
links.new(uv.outputs["UV"], texture.inputs["Vector"])
group = bpy.data.node_groups.new("Color interface", "ShaderNodeTree")
group.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
group.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
roughness = group.interface.new_socket(name="Roughness", in_out="INPUT", socket_type="NodeSocketFloat")
roughness.default_value = 0.62
group.interface.new_socket(name="Roughness", in_out="OUTPUT", socket_type="NodeSocketFloat")
entry = group.nodes.new("NodeGroupInput")
exit = group.nodes.new("NodeGroupOutput")
group.links.new(entry.outputs["Color"], exit.inputs["Color"])
math_node = group.nodes.new("ShaderNodeMath")
math_node.operation = "MULTIPLY"
math_node.inputs[1].default_value = 0.5
group.links.new(entry.outputs["Roughness"], math_node.inputs[0])
group.links.new(math_node.outputs[0], exit.inputs["Roughness"])
instance = nodes.new("ShaderNodeGroup")
instance.node_tree = group
links.new(texture.outputs["Color"], instance.inputs["Color"])
mix = nodes.new("ShaderNodeMixRGB")
mix.inputs[0].default_value = 0.25
mix.inputs[2].default_value = (0.05, 0.1, 0.6, 1.0)
links.new(instance.outputs["Color"], mix.inputs[1])
links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
links.new(instance.outputs["Roughness"], bsdf.inputs["Roughness"])
ramp = nodes.new("ShaderNodeValToRGB")
ramp.color_ramp.interpolation = "EASE"
ramp.color_ramp.elements[1].color = (0.2, 0.6, 0.9, 0.8)
links.new(texture.outputs["Alpha"], ramp.inputs["Fac"])
links.new(ramp.outputs["Color"], bsdf.inputs["Emission Color"])
bsdf.inputs["Emission Strength"].default_value = 0.2
uv.uv_map = "UV Map.!"
color = nodes.new("ShaderNodeVertexColor")
color.layer_name = "Color Map.!"
links.new(color.outputs["Color"], bsdf.inputs["Coat Tint"])
links.new(color.outputs["Alpha"], bsdf.inputs["Coat Weight"])
default_color = nodes.new("ShaderNodeVertexColor")
links.new(default_color.outputs["Color"], bsdf.inputs["Sheen Tint"])
plain = bpy.data.materials.new("No node tree")
plain.diffuse_color = (0.23, 0.47, 0.71, 0.35)
result = module.export_materials([mat, plain, None], destination)
assert len(result["materials"]) == 3 and len(result["dependencies"]) == 1
record = result["materials"][0]
assert all(n["type"] not in ("ShaderNodeGroup", "NodeGroupInput", "NodeGroupOutput") for n in record["nodes"])
assert record["color_management"]["color_interop_id"]
assert result["dependencies"][0]["packed"]
assert (destination / result["dependencies"][0]["filename"]).is_file()
(destination / "materials.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
geometry_module = import_blender("blend_export")
mesh = bpy.data.meshes.new("Attribute fixture")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
mesh.uv_layers.new(name="UV Map.!")
field = mesh.color_attributes.new(name="Color Map.!", type="FLOAT_COLOR", domain="CORNER")
for value in field.data:
    value.color = (0.2, 0.4, 0.6, 0.35)
mesh.color_attributes.render_color_index = 0
arrays = {}
# The Houdini probe materials read both fields by name (material_field_names of a Link import).
mesh_record = geometry_module._mesh_arrays(mesh, "fixture", arrays, referenced=("UV Map.!", "Color Map.!"))
assert mesh_record["default_color"] == "Color Map.!"
np.savez(destination / "mesh.npz", **arrays)
(destination / "mesh.json").write_text(json.dumps(mesh_record, indent=2))
print("BLEND_MATERIAL_EXPORT_PASSED", len(record["nodes"]), record["outputs"])
