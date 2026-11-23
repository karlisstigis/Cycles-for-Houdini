# SPDX-License-Identifier: Apache-2.0
"""Live parity check (tasks S1, S2, S3): the same Blender asset rendered by Blender's own Cycles and
by our Link mode in Houdini 22, for geometry-nodes instances (audit item 1), the texture space
behind Generated coordinates (audit item 2), in a second asset Blender's per-object values
(audit items 3, 5, 6: Object Info Random/Color/Alpha/Object Index, an Instancer attribute, an
emitter invisible to the camera but seen in a mirror, a holdout) and, in a third asset,
Random per Island and Pointiness (audit items 8, 9: islands joined by a loose edge, a bumpy
quad grid whose Pointiness is stretched by Map Range so small differences show) and, in a
fourth asset, hair (task H1, audit item 4: a Curves object with Hair Info Intercept/Random and a
per-curve attribute, geometry-nodes hair reading the surface UV, legacy particle hair with
children on a hidden emitter) and, in a fifth asset, point clouds (task H2: Point Info
Random/Radius/Position with varying radii, named point attributes, a geometry-nodes point cloud
reading Generated), in a sixth asset, volume shaders on ordinary meshes (task L1: Principled
Volume and Volume Scatter reading missing and mesh attributes as Blender's Cycles does) and, in a
seventh asset, hair and point radii stored per curve or as integers (task L1), compared per
region by coverage and colour (the media by mean colour per cube).

    hython tests/houdini_blend_sync_parity_smoke.py [--work DIR] [--report FILE] [--device CPU]
        [--blender EXE] [--threshold 0.02] [--points-performance N]

Needs the staged development build and Blender (the worker's executable). Everything is written
to a temporary work folder (the .blend, both images as .npy and .ppm, the report). The asset is a
collection with emission-only materials whose colour is the Generated coordinate, so lighting
and noise do not matter:
  - a 1 x 1 plane with an Array modifier (x2): the texture space is the box before modifiers
    and the flat side maps to 0.5,
  - a plane with a custom texture space,
  - a geometry-nodes object scattering primitive cubes (Instance on Points) and an object
    referenced through Object Info with As Instance off (both used to vanish in Link mode).
Blender renders it (96 x 96, CPU, 8 samples) through the same executable the worker uses; Houdini
renders the collection imported in Link mode with the same camera. The check is the mean absolute
difference of the RGB images, plus a per-region check that the instances are visible.
Prints a JSON summary and exits non-zero when a check failed.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import hou

SIZE = 96

BLEND_SCRIPT = r"""
import sys
import bpy
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Parity")
scene.collection.children.link(asset)


def generated_emission(name):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    coordinates = nodes.new("ShaderNodeTexCoord")
    emission = nodes.new("ShaderNodeEmission")
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(coordinates.outputs["Generated"], emission.inputs["Color"])
    links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


material = generated_emission("Generated")


def plane(name, location, size=1.0):
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
    obj = bpy.context.active_object
    obj.name = obj.data.name = name
    obj.data.materials.append(material)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    return obj


arr = plane("ArrayPlane", (-2.2, 1.2, 0.0))
arr.modifiers.new("Array", "ARRAY").count = 2
custom = plane("CustomSpace", (1.6, 1.2, 0.0), size=1.5)
custom.data.use_auto_texspace = False
custom.data.texspace_location = (0.2, -0.3, 0.0)
custom.data.texspace_size = (1.2, 0.6, 0.4)

bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0.0, 0.0, -30.0))
rock = bpy.context.active_object
rock.name = "Rock"
rock.data.materials.append(material)  # stays out of the asset; used through Object Info


def scatter(name, location, use_object):
    bpy.ops.mesh.primitive_plane_add(size=0.1, location=location)
    obj = bpy.context.active_object
    obj.name = name
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    tree = bpy.data.node_groups.new(name + "Tree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    grid = nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = 1.6
    grid.inputs["Size Y"].default_value = 0.8
    grid.inputs["Vertices X"].default_value = 3
    grid.inputs["Vertices Y"].default_value = 2
    points = nodes.new("GeometryNodeMeshToPoints")
    place = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(grid.outputs["Mesh"], points.inputs["Mesh"])
    links.new(points.outputs["Points"], place.inputs["Points"])
    if use_object:
        info = nodes.new("GeometryNodeObjectInfo")
        info.inputs["Object"].default_value = rock
        info.inputs["As Instance"].default_value = False
        links.new(info.outputs["Geometry"], place.inputs["Instance"])
    else:
        cube = nodes.new("GeometryNodeMeshCube")
        cube.inputs["Size"].default_value = (0.4, 0.3, 0.2)
        assign = nodes.new("GeometryNodeSetMaterial")
        assign.inputs["Material"].default_value = material
        links.new(cube.outputs["Mesh"], assign.inputs["Geometry"])
        links.new(assign.outputs["Geometry"], place.inputs["Instance"])
    links.new(place.outputs["Instances"], nodes.new("NodeGroupOutput").inputs["Geometry"])
    obj.modifiers.new("GN", "NODES").node_group = tree
    return obj


scatter("ScatterCubes", (-1.2, -1.3, 0.0), False)
scatter("ScatterRocks", (1.2, -1.3, 0.0), True)

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
scene.cycles.samples = 8
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

OBJECT_SCRIPT = r"""
import sys
import bpy
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("ObjectValues")
scene.collection.children.link(asset)


def material(name, build):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(build(nodes, links), output.inputs["Surface"])
    return result


def emission(nodes, links, color=None, strength=None):
    node = nodes.new("ShaderNodeEmission")
    if color is not None:
        links.new(color, node.inputs["Color"])
    if strength is not None:
        links.new(strength, node.inputs["Strength"])
    return node.outputs["Emission"]


def random_build(nodes, links):
    return emission(nodes, links, color=nodes.new("ShaderNodeObjectInfo").outputs["Random"])


def color_build(nodes, links):
    info = nodes.new("ShaderNodeObjectInfo")
    index = nodes.new("ShaderNodeMath")
    index.operation, index.inputs[1].default_value = "MULTIPLY", 0.1
    links.new(info.outputs["Object Index"], index.inputs[0])
    alpha = nodes.new("ShaderNodeMath")
    alpha.operation, alpha.inputs[1].default_value = "MULTIPLY", 0.5
    links.new(info.outputs["Alpha"], alpha.inputs[0])
    total = nodes.new("ShaderNodeMath")
    links.new(index.outputs[0], total.inputs[0])
    links.new(alpha.outputs[0], total.inputs[1])
    return emission(nodes, links, color=info.outputs["Color"], strength=total.outputs[0])


def instancer_build(nodes, links):
    attribute = nodes.new("ShaderNodeAttribute")
    attribute.attribute_type, attribute.attribute_name = "INSTANCER", "inst_color"
    return emission(nodes, links, color=attribute.outputs["Color"])


def green_build(nodes, links):
    node = nodes.new("ShaderNodeEmission")
    node.inputs["Color"].default_value = (0.1, 1.0, 0.2, 1.0)
    return node.outputs["Emission"]


def red_build(nodes, links):
    node = nodes.new("ShaderNodeEmission")
    node.inputs["Color"].default_value = (1.0, 0.1, 0.1, 1.0)
    return node.outputs["Emission"]


def mirror_build(nodes, links):
    node = nodes.new("ShaderNodeBsdfPrincipled")
    node.inputs["Base Color"].default_value = (1, 1, 1, 1)
    node.inputs["Metallic"].default_value = 1.0
    node.inputs["Roughness"].default_value = 0.0
    return node.outputs["BSDF"]


random_material = material("ObjectRandom", random_build)
color_material = material("ObjectColor", color_build)
instancer_material = material("InstancerColor", instancer_build)


def plane(name, location, size, used):
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
    obj = bpy.context.active_object
    obj.name = obj.data.name = name
    obj.data.materials.append(used)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    return obj


for x, name in zip((-2.6, -1.5, -0.4), ("Tile A", "Tile B", "Tile ü")):
    plane(name, (x, 2.4, 0.0), 0.8, random_material)


def scatter(name, location, used, store):
    bpy.ops.mesh.primitive_plane_add(size=0.1, location=location)
    obj = bpy.context.active_object
    obj.name = name
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    tree = bpy.data.node_groups.new(name + "Tree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    grid = nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = grid.inputs["Size Y"].default_value = 1.0
    grid.inputs["Vertices X"].default_value = grid.inputs["Vertices Y"].default_value = 2
    points = nodes.new("GeometryNodeMeshToPoints")
    place = nodes.new("GeometryNodeInstanceOnPoints")
    cube = nodes.new("GeometryNodeMeshCube")
    cube.inputs["Size"].default_value = (0.5, 0.5, 0.5)
    assign = nodes.new("GeometryNodeSetMaterial")
    assign.inputs["Material"].default_value = used
    links.new(grid.outputs["Mesh"], points.inputs["Mesh"])
    links.new(points.outputs["Points"], place.inputs["Points"])
    links.new(cube.outputs["Mesh"], assign.inputs["Geometry"])
    links.new(assign.outputs["Geometry"], place.inputs["Instance"])
    result = place.outputs["Instances"]
    if store:
        # A per-instance colour: the point position moved into 0.1 .. 1.1.
        offset = nodes.new("ShaderNodeVectorMath")
        offset.inputs[1].default_value = (0.6, 0.6, 0.3)
        links.new(nodes.new("GeometryNodeInputPosition").outputs["Position"], offset.inputs[0])
        attribute = nodes.new("GeometryNodeStoreNamedAttribute")
        attribute.data_type, attribute.domain = "FLOAT_COLOR", "INSTANCE"
        attribute.inputs["Name"].default_value = "inst_color"
        links.new(result, attribute.inputs["Geometry"])
        links.new(offset.outputs["Vector"], attribute.inputs["Value"])
        result = attribute.outputs["Geometry"]
    links.new(result, nodes.new("NodeGroupOutput").inputs["Geometry"])
    obj.modifiers.new("GN", "NODES").node_group = tree
    return obj


scatter("RandomScatter", (1.8, 2.4, 0.0), random_material, False)
scatter("InstancerScatter", (1.8, 0.0, 0.0), instancer_material, True)
tinted = plane("Tinted", (-2.0, 0.0, 0.0), 1.2, color_material)
tinted.color = (0.9, 0.3, 0.1, 0.6)
tinted.pass_index = 5
# A mirror reflecting an emitter that the camera does not see directly.
plane("Mirror", (-1.8, -2.2, 0.0), 1.6, material("Mirror", mirror_build))
ghost = plane("Ghost", (-1.8, -2.2, 3.0), 0.8, material("Green", green_build))
ghost.rotation_euler = (3.14159265, 0.0, 0.0)
ghost.visible_camera = False
# A holdout card in front of an emissive backdrop.
plane("Backdrop", (1.8, -2.2, 0.0), 1.6, material("Red", red_build))
plane("Hole", (1.8, -2.2, 0.5), 0.6, random_material).is_holdout = True

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
scene.cycles.samples = 8
scene.cycles.use_denoising = False
scene.cycles.use_adaptive_sampling = False
render.resolution_x = render.resolution_y = size
render.resolution_percentage = 100
render.image_settings.file_format = "OPEN_EXR"
render.image_settings.color_depth = "32"
render.filepath = image + ".exr"
bpy.ops.render.render(write_still=True)
loaded = bpy.data.images.load(image + ".exr")
pixels = np.array(loaded.pixels[:], np.float32).reshape(size, size, 4)[::-1]  # RGBA, top row first
np.save(image + ".npy", pixels)
print("BLENDER_RENDER_DONE")
"""

INTRINSIC_SCRIPT = r"""
import math
import sys
import bpy
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Intrinsics")
scene.collection.children.link(asset)


def material(name, output):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    geometry = nodes.new("ShaderNodeNewGeometry")
    emission = nodes.new("ShaderNodeEmission")
    value = geometry.outputs[output]
    if output == "Pointiness":
        stretch = nodes.new("ShaderNodeMapRange")
        stretch.inputs["From Min"].default_value = 0.4
        stretch.inputs["From Max"].default_value = 0.6
        links.new(value, stretch.inputs["Value"])
        value = stretch.outputs["Result"]
    links.new(value, emission.inputs["Color"])
    links.new(emission.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
    return result


def add(name, vertices, edges, faces, used):
    data = bpy.data.meshes.new(name)
    data.from_pydata(vertices, edges, faces)
    data.materials.append(used)
    obj = bpy.data.objects.new(name, data)
    asset.objects.link(obj)
    return obj


# Six 0.8 x 0.8 quads at y 2; the last two are joined only by a loose edge. Some quads list
# their corners backwards so union-find roots differ from the lowest vertex numbers.
vertices, faces = [], []
for k, x in enumerate((-2.6, -1.5, -0.4, 0.7, 1.8, 2.9)):
    corners = [(x - 0.4, 1.6, 0.0), (x + 0.4, 1.6, 0.0), (x + 0.4, 2.4, 0.0), (x - 0.4, 2.4, 0.0)]
    if k % 2:
        corners.reverse()
    faces.append(tuple(range(len(vertices), len(vertices) + 4)))
    vertices.extend(corners)
add("Islands", vertices, [(17, 20)], faces, material("RandomPerIsland", "Random Per Island"))
# A 16 x 16 quad grid with a bump and a crease; its quads are not planar.
count, grid = 16, []
for j in range(count):
    for i in range(count):
        x, y = -2.6 + 5.2 * i / (count - 1), -3.2 + 3.0 * j / (count - 1)
        grid.append((x, y, 0.9 * math.exp(-2.0 * (x * x + (y + 1.7) ** 2)) + 0.25 * abs(math.sin(2.0 * x))))
quads = [(j * count + i, j * count + i + 1, (j + 1) * count + i + 1, (j + 1) * count + i)
         for j in range(count - 1) for i in range(count - 1)]
add("Bump", grid, [], quads, material("Pointiness", "Pointiness"))

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
scene.cycles.samples = 8
scene.cycles.use_denoising = False
scene.cycles.use_adaptive_sampling = False
scene.view_settings.view_transform = "Standard"
render.resolution_x = render.resolution_y = size
render.resolution_percentage = 100
render.image_settings.file_format = "OPEN_EXR"
render.image_settings.color_depth = "32"
render.filepath = image + ".exr"
bpy.ops.render.render(write_still=True)
loaded = bpy.data.images.load(image + ".exr")
pixels = np.array(loaded.pixels[:], np.float32).reshape(size, size, 4)[::-1]  # RGBA, top row first
np.save(image + ".npy", pixels)
print("BLENDER_RENDER_DONE")
"""


HAIR_SCRIPT = r"""
import sys
import bpy
import bmesh
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.cycles_curves.shape = "THICK"
asset = bpy.data.collections.new("Hair")
scene.collection.children.link(asset)


def emission_material(name, build):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    combine = nodes.new("ShaderNodeCombineXYZ")  # Combine Color's mode is not translated yet
    emission = nodes.new("ShaderNodeEmission")
    build(nodes, links, combine)
    links.new(combine.outputs["Vector"], emission.inputs["Color"])
    links.new(emission.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
    return result


def strands_build(nodes, links, combine):
    info = nodes.new("ShaderNodeHairInfo")
    tint = nodes.new("ShaderNodeAttribute")
    tint.attribute_name = "tint"
    links.new(info.outputs["Intercept"], combine.inputs["X"])
    links.new(info.outputs["Random"], combine.inputs["Y"])
    links.new(tint.outputs["Fac"], combine.inputs["Z"])


def uv_build(nodes, links, combine, intercept=False):
    coordinates = nodes.new("ShaderNodeTexCoord")
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coordinates.outputs["UV"], split.inputs["Vector"])
    links.new(split.outputs["X"], combine.inputs["X"])
    links.new(split.outputs["Y"], combine.inputs["Y"])
    if intercept:
        links.new(nodes.new("ShaderNodeHairInfo").outputs["Intercept"], combine.inputs["Z"])


# Region 1 (top left): a Curves object, ten Catmull-Rom strands lying in the XY plane, with a
# radius, Hair Info Intercept/Random and a per-curve attribute in the material.
curves = bpy.data.hair_curves.new("StrandsData")
curves.add_curves([6] * 10)
positions = []
for k in range(10):
    for i in range(6):
        x = -3.2 + 2.6 * i / 5
        positions.append((x, 0.8 + 0.25 * k + 0.08 * np.sin(2.0 * x + k), 0.0))
curves.attributes["position"].data.foreach_set("vector", np.array(positions, np.float32).ravel())
curves.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", np.full(60, 0.06, np.float32))
curves.attributes.new("tint", "FLOAT", "CURVE").data.foreach_set("value", np.linspace(0.1, 1.0, 10, dtype=np.float32))
curves.materials.append(emission_material("Strands", strands_build))
asset.objects.link(bpy.data.objects.new("Strands", curves))

# Region 2 (top right): geometry-nodes hair on a surface that is itself not rendered; the
# surface UV is stored on the curves as surface_uv_coordinate (UV output of Texture Coordinate).
surface_mesh = bpy.data.meshes.new("SurfaceMesh")
editable = bmesh.new()
editable.loops.layers.uv.new("UVMap")
bmesh.ops.create_grid(editable, x_segments=2, y_segments=2, size=1.0, calc_uvs=True)
editable.to_mesh(surface_mesh)
editable.free()
surface = bpy.data.objects.new("Surface", surface_mesh)
surface.location, surface.scale = (1.75, 1.9, 0.0), (1.3, 1.2, 1.0)
surface.hide_render = True
scene.collection.objects.link(surface)
fur = bpy.data.objects.new("GNHair", bpy.data.hair_curves.new("GNHairData"))
asset.objects.link(fur)
tree = bpy.data.node_groups.new("HairTree", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
n, links = tree.nodes, tree.links
info = n.new("GeometryNodeObjectInfo")
info.transform_space = "RELATIVE"
info.inputs["Object"].default_value = surface
points = n.new("GeometryNodeDistributePointsOnFaces")
points.inputs["Density"].default_value = 10.0
line = n.new("GeometryNodeCurvePrimitiveLine")
line.inputs["End"].default_value = (0.35, 0.1, 0.0)
resample = n.new("GeometryNodeResampleCurve")
resample.inputs["Count"].default_value = 4
place = n.new("GeometryNodeInstanceOnPoints")
realize = n.new("GeometryNodeRealizeInstances")
uv = n.new("GeometryNodeInputNamedAttribute")
uv.data_type = "FLOAT_VECTOR"
uv.inputs["Name"].default_value = "UVMap"
store = n.new("GeometryNodeStoreNamedAttribute")
store.data_type, store.domain = "FLOAT2", "CURVE"
store.inputs["Name"].default_value = "surface_uv_coordinate"
drop = n.new("GeometryNodeRemoveAttribute")
drop.inputs["Name"].default_value = "UVMap"
radius = n.new("GeometryNodeSetCurveRadius")
radius.inputs["Radius"].default_value = 0.045
assign = n.new("GeometryNodeSetMaterial")
assign.inputs["Material"].default_value = emission_material("SurfaceUV", uv_build)
links.new(info.outputs["Geometry"], points.inputs["Mesh"])
links.new(line.outputs["Curve"], resample.inputs["Curve"])
links.new(points.outputs["Points"], place.inputs["Points"])
links.new(resample.outputs["Curve"], place.inputs["Instance"])
links.new(place.outputs["Instances"], realize.inputs["Geometry"])
links.new(realize.outputs["Geometry"], store.inputs["Geometry"])
links.new(uv.outputs["Attribute"], store.inputs["Value"])
links.new(store.outputs["Geometry"], drop.inputs["Geometry"])
links.new(drop.outputs["Geometry"], radius.inputs["Curve"])
links.new(radius.outputs["Curve"], assign.inputs["Geometry"])
links.new(assign.outputs["Geometry"], n.new("NodeGroupOutput").inputs["Geometry"])
fur.modifiers.new("GN", "NODES").node_group = tree
fur.data.materials.append(bpy.data.materials["SurfaceUV"])

# Region 3 (bottom): legacy particle hair with interpolated children on a hidden emitter; the
# strands grow along the emitter's X axis, the material reads the emitter UV and Intercept.
emitter_mesh = bpy.data.meshes.new("EmitterMesh")
editable = bmesh.new()
editable.loops.layers.uv.new("UVMap")
bmesh.ops.create_grid(editable, x_segments=4, y_segments=2, size=1.0, calc_uvs=True)
editable.to_mesh(emitter_mesh)
editable.free()
emitter_mesh.materials.append(emission_material("EmitterUV", lambda n, l, c: uv_build(n, l, c, True)))
emitter = bpy.data.objects.new("Emitter", emitter_mesh)
emitter.location, emitter.scale = (-0.5, -2.2, 0.0), (2.0, 0.75, 1.0)
emitter.show_instancer_for_render = False
asset.objects.link(emitter)
system = emitter.modifiers.new("Hair", "PARTICLE_SYSTEM").particle_system
system.seed = 3
settings = system.settings
settings.type, settings.count, settings.hair_length = "HAIR", 40, 0.7
settings.render_step = settings.display_step = 3
settings.use_advanced_hair = True
settings.normal_factor = 0.0
settings.object_align_factor = (1.0, 0.0, 0.0)
settings.root_radius, settings.tip_radius, settings.radius_scale = 1.0, 0.5, 0.11
settings.child_type = "INTERPOLATED"
settings.child_percent = settings.rendered_child_count = 3
settings.material = 1

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
scene.cycles.samples = 32
scene.cycles.use_denoising = False
scene.cycles.use_adaptive_sampling = False
scene.view_settings.view_transform = "Standard"
render.resolution_x = render.resolution_y = size
render.resolution_percentage = 100
render.image_settings.file_format = "OPEN_EXR"
render.image_settings.color_depth = "32"
render.filepath = image + ".exr"
bpy.ops.render.render(write_still=True)
loaded = bpy.data.images.load(image + ".exr")
pixels = np.array(loaded.pixels[:], np.float32).reshape(size, size, 4)[::-1]  # RGBA, top row first
np.save(image + ".npy", pixels)
print("BLENDER_RENDER_DONE")
"""


POINTS_SCRIPT = r"""
import sys
import bpy
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Points")
scene.collection.children.link(asset)


def emission_material(name, build):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    combine = nodes.new("ShaderNodeCombineXYZ")
    emission = nodes.new("ShaderNodeEmission")
    build(nodes, links, combine)
    links.new(combine.outputs["Vector"], emission.inputs["Color"])
    links.new(emission.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
    return result


def remap(nodes, links, socket, low, high, target):
    stretch = nodes.new("ShaderNodeMapRange")
    stretch.inputs["From Min"].default_value, stretch.inputs["From Max"].default_value = low, high
    links.new(socket, stretch.inputs["Value"])
    links.new(stretch.outputs["Result"], target)


def info_build(nodes, links, combine):
    info = nodes.new("ShaderNodePointInfo")
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(info.outputs["Position"], split.inputs["Vector"])
    links.new(info.outputs["Random"], combine.inputs["X"])
    remap(nodes, links, info.outputs["Radius"], 0.0, 0.25, combine.inputs["Y"])
    remap(nodes, links, split.outputs["Y"], 0.6, 3.2, combine.inputs["Z"])


def named_build(nodes, links, combine):
    heat = nodes.new("ShaderNodeAttribute")
    heat.attribute_name = "heat"
    tint = nodes.new("ShaderNodeAttribute")
    tint.attribute_name = "tint"
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(tint.outputs["Color"], split.inputs["Vector"])
    links.new(heat.outputs["Fac"], combine.inputs["X"])
    links.new(split.outputs["Y"], combine.inputs["Y"])
    links.new(split.outputs["Z"], combine.inputs["Z"])


def generated_build(nodes, links, combine):
    weight = nodes.new("ShaderNodeAttribute")
    weight.attribute_name = "weight"
    info = nodes.new("ShaderNodePointInfo")
    coordinates = nodes.new("ShaderNodeTexCoord")
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coordinates.outputs["Generated"], split.inputs["Vector"])
    links.new(weight.outputs["Fac"], combine.inputs["X"])
    links.new(info.outputs["Random"], combine.inputs["Y"])
    # No Generated attribute on points: Cycles uses the object-space position (x 0 .. 5, so
    # 0 .. 0.83 here; the bounds of the points would give 0 .. 1).
    remap(nodes, links, split.outputs["X"], 0.0, 6.0, combine.inputs["Z"])


rng = np.random.default_rng(5)
# Region 1 (top left): a point cloud object, 6 x 5 points with radii from 0.06 to 0.22, Point
# Info Random, Radius and Position in the material.
cloud = bpy.data.pointclouds.new("InfoData")
cloud.resize(30)
x, y = np.meshgrid(np.linspace(-3.1, -0.7, 6), np.linspace(0.7, 3.1, 5))
cloud.attributes["position"].data.foreach_set(
    "vector", np.stack([x.ravel(), y.ravel(), np.zeros(30)], axis=1).astype(np.float32).ravel()
)
cloud.attributes.new("radius", "FLOAT", "POINT").data.foreach_set(
    "value", np.linspace(0.06, 0.22, 30, dtype=np.float32)
)
cloud.materials.append(emission_material("PointInfo", info_build))
cloud.materials.append(emission_material("Unused", lambda n, l, c: None))  # Blender uses slot 1 only
asset.objects.link(bpy.data.objects.new("InfoPoints", cloud))

# Region 2 (top right): named point attributes (a float and a colour) drive the emission.
named = bpy.data.pointclouds.new("NamedData")
named.resize(36)
x, y = np.meshgrid(np.linspace(0.8, 3.0, 6), np.linspace(0.8, 3.0, 6))
named.attributes["position"].data.foreach_set(
    "vector", np.stack([x.ravel(), y.ravel(), np.zeros(36)], axis=1).astype(np.float32).ravel()
)
named.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", np.full(36, 0.15, np.float32))
named.attributes.new("heat", "FLOAT", "POINT").data.foreach_set("value", rng.random(36).astype(np.float32))
named.attributes.new("tint", "FLOAT_COLOR", "POINT").data.foreach_set(
    "color", np.concatenate([rng.random((36, 3)), np.ones((36, 1))], axis=1).astype(np.float32).ravel()
)
named.materials.append(emission_material("Named", named_build))
asset.objects.link(bpy.data.objects.new("NamedPoints", named))

# Region 3 (bottom): a geometry-nodes point cloud (Mesh to Points of a grid, a stored random
# "weight") on a mesh object; the material reads it, Point Info Random and Generated.
holder = bpy.data.objects.new("GNPoints", bpy.data.meshes.new("GNPointsMesh"))
holder.location = (-3.0, -2.2, 0.0)
asset.objects.link(holder)
tree = bpy.data.node_groups.new("PointsTree", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
n, links = tree.nodes, tree.links
grid = n.new("GeometryNodeMeshGrid")
grid.inputs["Size X"].default_value, grid.inputs["Size Y"].default_value = 5.0, 1.4
grid.inputs["Vertices X"].default_value, grid.inputs["Vertices Y"].default_value = 11, 4
shift = n.new("GeometryNodeTransform")
shift.inputs["Translation"].default_value = (2.5, 0.0, 0.0)
to_points = n.new("GeometryNodeMeshToPoints")
to_points.inputs["Radius"].default_value = 0.13
store = n.new("GeometryNodeStoreNamedAttribute")
store.data_type, store.domain = "FLOAT", "POINT"
store.inputs["Name"].default_value = "weight"
random = n.new("FunctionNodeRandomValue")
random.inputs["Seed"].default_value = 4
assign = n.new("GeometryNodeSetMaterial")
assign.inputs["Material"].default_value = emission_material("GNPoints", generated_build)
links.new(grid.outputs["Mesh"], shift.inputs["Geometry"])
links.new(shift.outputs["Geometry"], to_points.inputs["Mesh"])
links.new(to_points.outputs["Points"], store.inputs["Geometry"])
links.new(random.outputs["Value"], store.inputs["Value"])
links.new(store.outputs["Geometry"], assign.inputs["Geometry"])
links.new(assign.outputs["Geometry"], n.new("NodeGroupOutput").inputs["Geometry"])
holder.modifiers.new("GN", "NODES").node_group = tree

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
scene.cycles.samples = 32
scene.cycles.use_denoising = False
scene.cycles.use_adaptive_sampling = False
scene.view_settings.view_transform = "Standard"
render.resolution_x = render.resolution_y = size
render.resolution_percentage = 100
render.image_settings.file_format = "OPEN_EXR"
render.image_settings.color_depth = "32"
render.filepath = image + ".exr"
bpy.ops.render.render(write_still=True)
loaded = bpy.data.images.load(image + ".exr")
pixels = np.array(loaded.pixels[:], np.float32).reshape(size, size, 4)[::-1]  # RGBA, top row first
np.save(image + ".npy", pixels)
print("BLENDER_RENDER_DONE")
"""


# --points-performance N: one point cloud of N points (radius, a float and a colour attribute),
# timed through Link import and a 1-sample render; no image comparison.
POINTS_PERFORMANCE_SCRIPT = r"""
import sys
import time
import bpy
import numpy as np

blend, image, size, count = sys.argv[sys.argv.index("--") + 1 :][:4]
count = int(count)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("ManyPoints")
scene.collection.children.link(asset)
rng = np.random.default_rng(1)
cloud = bpy.data.pointclouds.new("ManyData")
cloud.resize(count)
cloud.attributes["position"].data.foreach_set("vector", (rng.random(count * 3, np.float32) * 6 - 3).ravel())
cloud.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", rng.random(count, np.float32) * 0.004)
cloud.attributes.new("heat", "FLOAT", "POINT").data.foreach_set("value", rng.random(count, np.float32))
cloud.attributes.new("tint", "FLOAT_COLOR", "POINT").data.foreach_set("color", rng.random(count * 4, np.float32))
material = bpy.data.materials.new("Heat")
material.use_nodes = True
nodes, links = material.node_tree.nodes, material.node_tree.links
nodes.clear()
heat = nodes.new("ShaderNodeAttribute")
heat.attribute_name = "heat"
emission = nodes.new("ShaderNodeEmission")
links.new(heat.outputs["Color"], emission.inputs["Color"])
links.new(emission.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
cloud.materials.append(material)
asset.objects.link(bpy.data.objects.new("ManyPoints", cloud))
bpy.ops.wm.save_as_mainfile(filepath=blend)
np.save(image + ".npy", np.zeros((1, 1, 4), np.float32))
print("BLENDER_RENDER_DONE")
"""


# Task L1 (sixth asset "Media"): volume shaders on ordinary meshes. Blender's Cycles renders a
# mesh medium whose shader reads a volume attribute the mesh does not have with Cycles' own
# "attribute not found" values (Principled Volume: density and colour multipliers 1, the
# temperature input unscaled; an Attribute node: its Missing value, 0). A white emissive floor
# under six 1.6 m cubes, top row left to right: Principled Volume density 0.5, density 2.0,
# density 1.0 with a blue colour; bottom row: Volume Scatter with density 2 x Attribute.Fac + 0.4
# for a missing attribute, for a "density" POINT attribute with values 1..3 on the cube, and
# Principled Volume (density 1) on a cube with a constant "density" POINT attribute of 4.
MEDIA_SCRIPT = r"""
import sys
import bpy
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Media")
scene.collection.children.link(asset)


def material(name):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes = result.node_tree.nodes
    nodes.clear()
    return result, nodes, result.node_tree.links, nodes.new("ShaderNodeOutputMaterial")


def principled(name, density, color=None):
    result, nodes, links, output = material(name)
    volume = nodes.new("ShaderNodeVolumePrincipled")  # Density Attribute "density" (the default)
    volume.inputs["Density"].default_value = density
    if color is not None:
        volume.inputs["Color"].default_value = color + (1.0,)
    links.new(volume.outputs["Volume"], output.inputs["Volume"])
    return result


def scatter(name, attribute):
    result, nodes, links, output = material(name)
    field = nodes.new("ShaderNodeAttribute")
    field.attribute_name = attribute
    scale = nodes.new("ShaderNodeMath")
    scale.operation = "MULTIPLY_ADD"
    scale.inputs[1].default_value, scale.inputs[2].default_value = 2.0, 0.4
    volume = nodes.new("ShaderNodeVolumeScatter")
    links.new(field.outputs["Fac"], scale.inputs[0])
    links.new(scale.outputs["Value"], volume.inputs["Density"])
    links.new(volume.outputs["Volume"], output.inputs["Volume"])
    return result


def add(obj, used):
    obj.data.materials.append(used)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    asset.objects.link(obj)
    return obj


floor, nodes, links, output = material("Floor")
glow = nodes.new("ShaderNodeEmission")
glow.inputs["Strength"].default_value = 1.0
links.new(glow.outputs["Emission"], output.inputs["Surface"])
bpy.ops.mesh.primitive_plane_add(size=8.0, location=(0.0, 0.0, 0.0))
add(bpy.context.active_object, floor).name = "Floor"
rng = np.random.default_rng(11)
cells = (
    ("Thin", -2.3, 1.25, principled("Thin", 0.5), None),
    ("Thick", 0.0, 1.25, principled("Thick", 2.0), None),
    ("Blue", 2.3, 1.25, principled("Blue", 1.0, (0.2, 0.5, 1.0)), None),
    ("Missing", -2.3, -1.25, scatter("Missing", "missing_field"), None),
    ("Varying", 0.0, -1.25, scatter("Varying", "density"), "varying"),
    ("Constant", 2.3, -1.25, principled("Constant", 1.0), "constant"),
)
for name, x, y, used, attribute in cells:
    bpy.ops.mesh.primitive_cube_add(size=1.6, location=(x, y, 1.0))
    obj = add(bpy.context.active_object, used)
    obj.name = name
    if attribute:
        count = len(obj.data.vertices)
        values = rng.uniform(1.0, 3.0, count) if attribute == "varying" else np.full(count, 4.0)
        obj.data.attributes.new("density", "FLOAT", "POINT").data.foreach_set("value", values.astype(np.float32))

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
scene.cycles.samples = 64
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

# Task L1 (seventh asset "Radii"): Blender's Cycles reads "radius" through lookup<float>("radius",
# Point), so a radius stored per curve or as an integer is used too (curves.cpp
# export_hair_curves, pointcloud.cpp export_pointcloud). Top left: three poly hair curves with a
# CURVE-domain FLOAT radius 0.08, 0.15, 0.25; top right: one hair curve with an INT POINT radius 1;
# bottom: a point cloud with an INT radius 1. Emission-only colours.
RADII_SCRIPT = r"""
import sys
import bpy
import numpy as np

blend, image, size = sys.argv[sys.argv.index("--") + 1 :][:3]
size = int(size)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.cycles_curves.shape = "THICK"
asset = bpy.data.collections.new("Radii")
scene.collection.children.link(asset)


def emission(name, color):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes, links = result.node_tree.nodes, result.node_tree.links
    nodes.clear()
    glow = nodes.new("ShaderNodeEmission")
    glow.inputs["Color"].default_value = color + (1.0,)
    links.new(glow.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
    return result


def radius(data, kind, domain, values):
    if "radius" in data.attributes:
        data.attributes.remove(data.attributes["radius"])
    data.attributes.new("radius", kind, domain).data.foreach_set("value", values)


def hair(name, points, kind, domain, values, color):
    data = bpy.data.hair_curves.new(name + "Data")
    data.add_curves([2] * (len(points) // 2))
    data.attributes["position"].data.foreach_set("vector", np.asarray(points, np.float32).ravel())
    data.set_types(type="POLY")
    radius(data, kind, domain, values)
    data.materials.append(emission(name, color))
    asset.objects.link(bpy.data.objects.new(name, data))


hair(
    "PerCurve",
    [(-3.0, y, 0.5) if end == 0 else (-0.6, y, 0.5) for y in (2.8, 2.1, 1.2) for end in (0, 1)],
    "FLOAT",
    "CURVE",
    [0.08, 0.15, 0.25],
    (1.0, 0.2, 0.1),
)
hair("Integer", [(1.2, 2.0, 0.5), (2.2, 2.0, 0.5)], "INT", "POINT", [1, 1], (0.1, 1.0, 0.2))
cloud = bpy.data.pointclouds.new("IntegerPointsData")
cloud.resize(2)
cloud.attributes["position"].data.foreach_set("vector", np.array([(-2.0, -2.0, 0.0), (1.2, -2.0, 0.0)], np.float32).ravel())
radius(cloud, "INT", "POINT", [1, 1])
cloud.materials.append(emission("IntegerPoints", (0.2, 0.3, 1.0)))
asset.objects.link(bpy.data.objects.new("IntegerPoints", cloud))

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
scene.cycles.samples = 32
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


def intrinsic_checks(check, reference, ours, threshold):
    """Task S3: Random per Island per island and Pointiness over a bumpy grid, Blender's Cycles
    vs Link (Blender v5.3.0 attr_create_random_per_island / attr_create_pointiness)."""
    import numpy as np

    rgb = slice(0, 3)
    islands = []
    for x in (-2.6, -1.5, -0.4, 0.7, 1.8, 2.9):
        row, column = object_values_pixel(x, 2.0)
        window = (slice(row - 2, row + 3), slice(column - 2, column + 3), 0)
        islands.append(
            dict(
                blender=round(float(reference[window].mean()), 6),
                houdini=round(float(ours[window].mean()), 6),
                houdini_spread=round(float(ours[window].max() - ours[window].min()), 7),
            )
        )
    print("ISLANDS:" + json.dumps(islands))
    check(
        "Random per Island: Blender's value on every island, one value per island",
        all(abs(i["blender"] - i["houdini"]) < 1e-4 and i["houdini_spread"] < 1e-5 for i in islands),
        islands,
    )
    values = [i["houdini"] for i in islands]
    check(
        "Random per Island: the loose edge joins the last two quads; the others differ",
        abs(values[4] - values[5]) < 1e-6 and len({round(v, 5) for v in values[:5]}) == 5,
        values,
    )
    region = (slice(SIZE * 55 // 96, SIZE * 95 // 96), slice(SIZE * 4 // 96, SIZE * 92 // 96))
    a, b = reference[region][..., rgb], ours[region][..., rgb]
    bump = dict(
        mean_abs_difference=round(float(np.abs(a - b).mean()), 5),
        max_abs_difference=round(float(np.abs(a - b).max()), 5),
        blender_mean=round(float(a.mean()), 5),
        houdini_mean=round(float(b.mean()), 5),
        blender_spread=round(float(a.max() - a.min()), 4),
    )
    print("POINTINESS:" + json.dumps(bump))
    check(
        f"Pointiness (stretched x5 by Map Range): mean absolute difference < {threshold / 4}",
        bump["blender_spread"] > 0.3 and bump["mean_abs_difference"] < threshold / 4,
        bump,
    )


def hair_checks(check, reference, ours, threshold):
    """Task H1: hair (a Curves object, geometry-nodes hair with the surface UV, legacy particle
    hair with children), Blender's Cycles vs Link, per region: coverage and colour."""
    import numpy as np

    rgb = slice(0, 3)
    regions = dict(
        curves_object=(slice(2, 42), slice(2, 46)),
        geometry_nodes_hair=(slice(2, 42), slice(50, 95)),
        particle_hair=(slice(60, 94), slice(10, 90)),
    )
    for name, (rows, columns) in regions.items():
        a, b = reference[rows, columns][..., rgb], ours[rows, columns][..., rgb]
        covered_a, covered_b = a.max(axis=2) > 0.05, b.max(axis=2) > 0.05
        both = covered_a & covered_b
        numbers = dict(
            mean_abs_difference=round(float(np.abs(a - b).mean()), 5),
            covered_blender=round(float(covered_a.mean()), 4),
            covered_houdini=round(float(covered_b.mean()), 4),
            covered_overlap=round(float(both.sum() / max(1, (covered_a | covered_b).sum())), 4),
            mean_blender=a.mean(axis=(0, 1)).round(4).tolist(),
            mean_houdini=b.mean(axis=(0, 1)).round(4).tolist(),
            covered_mean_difference=round(float(np.abs(a[both] - b[both]).mean()), 5) if both.any() else None,
        )
        print("HAIR_" + name.upper() + ":" + json.dumps(numbers))
        check(
            f"hair {name}: covered in both (coverage within 10%, overlap > 0.8), mean absolute difference "
            f"< {threshold}, covered-pixel difference < {2 * threshold}",
            numbers["covered_blender"] > 0.04
            and abs(numbers["covered_houdini"] - numbers["covered_blender"]) <= 0.1 * numbers["covered_blender"]
            and numbers["covered_overlap"] > 0.8
            and numbers["mean_abs_difference"] < threshold
            and numbers["covered_mean_difference"] < 2 * threshold,
            numbers,
        )
        # Colour per channel: Intercept/Random/per-curve attribute, surface UV, emitter UV/Intercept.
        means = np.array(numbers["mean_houdini"]), np.array(numbers["mean_blender"])
        check(
            f"hair {name}: mean colour per channel within 2% (or 0.002) of Blender's",
            bool((np.abs(means[0] - means[1]) <= np.maximum(0.02 * means[1], 0.002)).all()),
            dict(houdini=numbers["mean_houdini"], blender=numbers["mean_blender"]),
        )


def points_checks(check, reference, ours, threshold):
    """Task H2: point clouds (Point Info Random/Radius/Position with varying radii, named point
    attributes, a geometry-nodes point cloud reading Generated), Blender's Cycles vs Link, per
    region: coverage and colour."""
    import numpy as np

    rgb = slice(0, 3)
    regions = dict(
        point_info=(slice(2, 46), slice(2, 46)),
        named_attributes=(slice(2, 46), slice(50, 95)),
        geometry_nodes_points=(slice(60, 94), slice(2, 85)),
    )
    for name, (rows, columns) in regions.items():
        a, b = reference[rows, columns][..., rgb], ours[rows, columns][..., rgb]
        covered_a, covered_b = a.max(axis=2) > 0.05, b.max(axis=2) > 0.05
        both = covered_a & covered_b
        numbers = dict(
            mean_abs_difference=round(float(np.abs(a - b).mean()), 5),
            covered_blender=round(float(covered_a.mean()), 4),
            covered_houdini=round(float(covered_b.mean()), 4),
            covered_overlap=round(float(both.sum() / max(1, (covered_a | covered_b).sum())), 4),
            mean_blender=a.mean(axis=(0, 1)).round(4).tolist(),
            mean_houdini=b.mean(axis=(0, 1)).round(4).tolist(),
            covered_mean_difference=round(float(np.abs(a[both] - b[both]).mean()), 5) if both.any() else None,
        )
        print("POINTS_" + name.upper() + ":" + json.dumps(numbers))
        check(
            f"points {name}: covered in both (coverage within 10%, overlap > 0.8), mean absolute difference "
            f"< {threshold}, covered-pixel difference < {2 * threshold}",
            numbers["covered_blender"] > 0.04
            and abs(numbers["covered_houdini"] - numbers["covered_blender"]) <= 0.1 * numbers["covered_blender"]
            and numbers["covered_overlap"] > 0.8
            and numbers["mean_abs_difference"] < threshold
            and numbers["covered_mean_difference"] < 2 * threshold,
            numbers,
        )
        means = np.array(numbers["mean_houdini"]), np.array(numbers["mean_blender"])
        check(
            f"points {name}: mean colour per channel within 2% (or 0.002) of Blender's",
            bool((np.abs(means[0] - means[1]) <= np.maximum(0.02 * means[1], 0.002)).all()),
            dict(houdini=numbers["mean_houdini"], blender=numbers["mean_blender"]),
        )


MEDIA_CELLS = dict(
    principled_density_0_5=(-2.3, 1.25),
    principled_density_2=(0.0, 1.25),
    principled_blue=(2.3, 1.25),
    scatter_missing_attribute=(-2.3, -1.25),
    scatter_point_attribute=(0.0, -1.25),
    principled_constant_point_attribute=(2.3, -1.25),
)


def media_region(x, y):
    """Pixels whose camera rays cross the cube at (x, y) through its top and bottom faces (cubes
    1.6 m wide from z 0.2 to 1.8, camera at z 10, 7.2 m wide at the floor)."""
    import numpy as np

    scale = SIZE / 7.2

    def pixels(centre, sign):
        low, high = sorted(sign * (centre + offset) * 10 / 9 * scale + SIZE / 2 for offset in (-0.5, 0.5))
        return slice(int(np.ceil(low)), int(np.floor(high)))

    return pixels(y, -1), pixels(x, 1)


def media_checks(check, reference, ours, threshold, diagnostics):
    """Task L1: mesh media reading missing or mesh attributes, Blender's Cycles vs Link, per cube:
    the mean colour through the cube (fog over the emissive floor)."""
    import numpy as np

    for name, (x, y) in MEDIA_CELLS.items():
        rows, columns = media_region(x, y)
        a, b = reference[rows, columns].mean(axis=(0, 1)), ours[rows, columns].mean(axis=(0, 1))
        numbers = dict(
            pixels=int((rows.stop - rows.start) * (columns.stop - columns.start)),
            mean_blender=a.round(4).tolist(),
            mean_houdini=b.round(4).tolist(),
            relative=(np.abs(b - a) / np.maximum(a, 1e-6)).round(4).tolist(),
        )
        print("MEDIA_" + name.upper() + ":" + json.dumps(numbers))
        check(
            f"media {name}: mean colour per channel within {2.5 * threshold:.0%} (or 0.01) of Blender's",
            bool((np.abs(b - a) <= np.maximum(2.5 * threshold * a, 0.01)).all()),
            numbers,
        )
    floor = reference[2:8, 40:56].mean()
    for name in ("principled_density_0_5", "principled_density_2", "principled_blue", "scatter_missing_attribute"):
        rows, columns = media_region(*MEDIA_CELLS[name])
        check(
            f"media {name}: Blender renders fog (the floor seen through it differs by > 5%)",
            abs(float(reference[rows, columns].mean()) / float(floor) - 1) > 0.05,
            dict(floor=round(float(floor), 4), cube=round(float(reference[rows, columns].mean()), 4)),
        )
    flagged = [
        d for d in diagnostics if d.get("code") in ("material.volume_source_unavailable", "material.geometry_binding")
    ]
    check("media: no volume-source or geometry-binding diagnostic in Link mode", not flagged, flagged)
    # Blender says nothing about mesh attributes a medium does not read, so Link does not either
    # (the "Varying" and "Constant" cubes carry a "density" point attribute).
    notes = [d for d in diagnostics if d.get("code") == "material.mesh_volume_attribute"]
    check("media: no mesh-volume-attribute note in Link mode", not notes, notes)


# The native counterpart of the six "Media" cubes (Blender (x, y, z) is Houdini (x, z, -y)), like
# for like: Houdini boxes with native Cycles Volume Materials over an emissive floor. Principled
# Volume at Density 0.5, 2.0 and 1.0 with a blue Color (default Density Attribute "density", which a
# mesh never supplies to a medium); Volume Scatter with density 2 x Attribute.Fac + 0.4 for a
# missing attribute and for "density" on a box carrying point values 1..3; Principled Volume
# (Density 1.0) on a box with a constant point "density" = 4. The two "density" boxes carry a mesh
# attribute the medium must not read (as in Blender); each gets one informational source note.
NATIVE_MEDIA = dict(
    principled_density_0_5=dict(shader="principled_volume", density=0.5),
    principled_density_2=dict(shader="principled_volume", density=2.0),
    principled_blue=dict(shader="principled_volume", density=1.0, color=(0.2, 0.5, 1.0)),
    scatter_missing_attribute=dict(shader="scatter_volume", field="missing_field"),
    scatter_point_attribute=dict(shader="scatter_volume", field="density", values="varying"),
    principled_constant_point_attribute=dict(shader="principled_volume", density=1.0, values="constant"),
)


def native_media_scene():
    import cycles_houdini as ch

    obj = hou.node("/obj")
    floor_material = ch.create_material(name="native_floor", shader="emission")
    floor_shader = floor_material.node("surface_shader")
    floor_shader.parmTuple("color").set((1.0, 1.0, 1.0))
    floor_shader.parm("strength").set(1.0)
    floor = obj.createNode("geo", "native_floor", run_init_scripts=False)
    grid = floor.createNode("grid")
    grid.parmTuple("size").set((8.0, 8.0))
    grid.parm("rows").set(2)
    grid.parm("cols").set(2)
    grid.setDisplayFlag(True)
    grid.setRenderFlag(True)
    floor.parm("shop_materialpath").set(floor_material.path())
    for name, spec in NATIVE_MEDIA.items():
        x, y = MEDIA_CELLS[name]
        material = ch.create_material(name="native_" + name, shader=spec["shader"])
        shader = material.node("volume_shader")
        if spec["shader"] == "principled_volume":
            shader.parm("density").set(spec["density"])
            if "color" in spec:
                shader.parmTuple("color").set(spec["color"])
        else:  # replace create_material's Volume Info density with Attribute.Fac x 2 + 0.4
            for child in material.children():
                if child.name() in ("volume_fields", "density_scale"):
                    child.destroy()
            field = material.createNode("cycles::attribute::1", "field")
            field.parm("attribute").set(spec["field"])
            scale = material.createNode("cycles::math::1", "scale")
            scale.parm("math_type").set("multiply_add")
            scale.parm("value2").set(2.0)
            scale.parm("value3").set(0.4)
            scale.setNamedInput("value1", field, "fac")
            shader.setNamedInput("density", scale, "value")
        geo = obj.createNode("geo", "native_" + name, run_init_scripts=False)
        box = geo.createNode("box")
        box.parmTuple("size").set((1.6, 1.6, 1.6))
        box.parmTuple("t").set((x, 1.0, -y))
        last = box
        if "values" in spec:
            last = geo.createNode("attribwrangle")
            last.setFirstInput(box)
            last.parm("snippet").set(
                "f@density = 4.0;" if spec["values"] == "constant" else "f@density = fit01(rand(@ptnum), 1.0, 3.0);"
            )
        last.setDisplayFlag(True)
        last.setRenderFlag(True)
        geo.parm("shop_materialpath").set(material.path())


def native_media_checks(check, reference, ours, threshold, diagnostics):
    """Task L1: native Houdini boxes with Principled Volume vs Blender's render of the same cubes."""
    import numpy as np

    for name in NATIVE_MEDIA:
        rows, columns = media_region(*MEDIA_CELLS[name])
        a, b = reference[rows, columns].mean(axis=(0, 1)), ours[rows, columns].mean(axis=(0, 1))
        numbers = dict(mean_blender=a.round(4).tolist(), mean_houdini=b.round(4).tolist())
        print("NATIVE_MEDIA_" + name.upper() + ":" + json.dumps(numbers))
        check(
            f"native media {name}: mean colour per channel within {2.5 * threshold:.0%} (or 0.01) of Blender's",
            bool((np.abs(b - a) <= np.maximum(2.5 * threshold * a, 0.01)).all()),
            numbers,
        )
    recoveries = [
        d for d in diagnostics if d.get("code") in ("material.volume_source_unavailable", "material.geometry_binding")
    ]
    check("native media: no volume-source or geometry-binding diagnostic", not recoveries, recoveries)
    notes = [d for d in diagnostics if d.get("code") == "material.mesh_volume_attribute"]
    expected = {"/obj/native_scatter_point_attribute", "/obj/native_principled_constant_point_attribute"}
    noted = {d.get("message", "").split(": ", 1)[0] for d in notes}
    check(
        "native media: one info note per box whose point attribute 'density' the medium does not read",
        len(notes) == 2
        and noted == expected
        and all(d.get("severity") == "info" and d.get("attribute") == "density" for d in notes),
        notes,
    )


def radii_checks(check, reference, ours, threshold):
    """Task L1: per-curve and integer radii on hair and an integer point radius, Blender's Cycles
    vs Link, per region: coverage and colour."""
    import numpy as np

    regions = dict(
        hair_per_curve_radius=(slice(0, 47), slice(0, 47)),
        hair_integer_radius=(slice(0, 47), slice(49, 96)),
        points_integer_radius=(slice(49, 96), slice(0, 96)),
    )
    for name, (rows, columns) in regions.items():
        a, b = reference[rows, columns], ours[rows, columns]
        covered_a, covered_b = a.max(axis=2) > 0.05, b.max(axis=2) > 0.05
        both = covered_a & covered_b
        numbers = dict(
            mean_abs_difference=round(float(np.abs(a - b).mean()), 5),
            covered_blender=round(float(covered_a.mean()), 4),
            covered_houdini=round(float(covered_b.mean()), 4),
            covered_overlap=round(float(both.sum() / max(1, (covered_a | covered_b).sum())), 4),
            mean_blender=a.mean(axis=(0, 1)).round(4).tolist(),
            mean_houdini=b.mean(axis=(0, 1)).round(4).tolist(),
        )
        print("RADII_" + name.upper() + ":" + json.dumps(numbers))
        check(
            f"radii {name}: covered in both (coverage within 5%, overlap > 0.9), mean absolute difference "
            f"< {threshold}",
            numbers["covered_blender"] > 0.05
            and abs(numbers["covered_houdini"] - numbers["covered_blender"]) <= 0.05 * numbers["covered_blender"]
            and numbers["covered_overlap"] > 0.9
            and numbers["mean_abs_difference"] < threshold,
            numbers,
        )


def object_values_pixel(x, y, z=0.0):
    """(row, column) of a Blender world point in the 96 x 96 image (camera at z 10, 50 mm, 36 mm)."""
    half = 0.36 * (10.0 - z)
    return int((half - y) / (2 * half) * SIZE), int((x + half) / (2 * half) * SIZE)


def object_values_checks(check, reference, ours, threshold):
    """Task S2: Object Info Random / Color / Alpha / Object Index, an Instancer attribute, a
    camera-invisible emitter seen in a mirror and a holdout, Blender's Cycles vs Link."""
    import numpy as np

    from cycles_houdini.blender.blend_ids import object_random_id

    def at(image, x, y, z=0.0):
        row, column = object_values_pixel(x, y, z)
        return image[row - 1 : row + 2, column - 1 : column + 2].reshape(-1, image.shape[2]).mean(axis=0)

    rgb = slice(0, 3)
    numbers = dict(mean_abs_difference=round(float(np.abs(ours[..., rgb] - reference[..., rgb]).mean()), 5))
    print("OBJECT_VALUES:" + json.dumps(numbers))
    check(f"object values: mean absolute difference < {threshold}", numbers["mean_abs_difference"] < threshold, numbers)
    tiles = {}
    for x, name in zip((-2.6, -1.5, -0.4), ("Tile A", "Tile B", "Tile ü")):
        expected = float(np.float32(object_random_id(name)) * np.float32(1.0 / 0xFFFFFFFF))
        tiles[name] = dict(
            expected=round(expected, 6),
            blender=round(float(at(reference, x, 2.4)[0]), 6),
            houdini=round(float(at(ours, x, 2.4)[0]), 6),
        )
    check(
        "Object Info Random of separate objects: Blender's value (hash of the name) in both",
        all(
            abs(v["blender"] - v["expected"]) < 1e-4 and abs(v["houdini"] - v["expected"]) < 1e-4
            for v in tiles.values()
        ),
        tiles,
    )
    centres = [(1.8 + dx, 2.4 + dy) for dx in (-0.5, 0.5) for dy in (-0.5, 0.5)]
    instances = [
        (round(float(at(reference, x, y, 0.25)[0]), 5), round(float(at(ours, x, y, 0.25)[0]), 5)) for x, y in centres
    ]
    check(
        "Object Info Random of scattered instances: equal per instance, all different",
        all(abs(a - b) < 1e-3 for a, b in instances) and len({a for a, _ in instances}) == 4,
        instances,
    )
    expected = np.array([0.9, 0.3, 0.1]) * (5 * 0.1 + 0.6 * 0.5)
    tinted = dict(
        expected=expected.round(5).tolist(),
        blender=at(reference, -2.0, 0.0)[rgb].round(5).tolist(),
        houdini=at(ours, -2.0, 0.0)[rgb].round(5).tolist(),
    )
    check(
        "Object colour, alpha and pass index (Object Info Color/Alpha/Object Index)",
        np.allclose(tinted["blender"], expected, atol=1e-3) and np.allclose(tinted["houdini"], expected, atol=1e-3),
        tinted,
    )
    centres = [(1.8 + dx, dy) for dx in (-0.5, 0.5) for dy in (-0.5, 0.5)]
    attributes = [
        (at(reference, x, y, 0.25)[rgb].round(4).tolist(), at(ours, x, y, 0.25)[rgb].round(4).tolist())
        for x, y in centres
    ]
    check(
        "Instancer attribute per instance (Attribute node, type Instancer)",
        all(np.allclose(a, b, atol=2e-3) and max(a) > 0.05 for a, b in attributes),
        attributes,
    )
    # The mirror at z 0 shows the emitter's virtual image at z -3; the emitter itself (z 3, camera
    # rays off) would be seen elsewhere, around object_values_pixel(-1.8, -2.2, 3).
    row, column = object_values_pixel(-1.8, -2.2)
    window = (slice(row - 10, row + 11), slice(column - 10, column + 11))
    mirror = dict(
        mean_abs_difference=round(float(np.abs(ours[window][..., rgb] - reference[window][..., rgb]).mean()), 5),
        reflection_blender=round(float(at(reference, -1.8, -2.2, -3.0)[1]), 4),
        reflection_houdini=round(float(at(ours, -1.8, -2.2, -3.0)[1]), 4),
        direct_blender=round(float(at(reference, -1.8, -2.2, 3.0)[1]), 4),
        direct_houdini=round(float(at(ours, -1.8, -2.2, 3.0)[1]), 4),
    )
    check(
        "camera-invisible emitter: seen in the mirror, not directly",
        mirror["reflection_blender"] > 0.3
        and mirror["reflection_houdini"] > 0.3
        and mirror["direct_blender"] < 0.05
        and mirror["direct_houdini"] < 0.05
        and mirror["mean_abs_difference"] < 2 * threshold,
        mirror,
    )
    hole = dict(
        alpha_blender=round(float(at(reference, 1.8, -2.2, 0.5)[3]), 4) if reference.shape[2] > 3 else None,
        alpha_houdini=round(float(at(ours, 1.8, -2.2, 0.5)[3]), 4) if ours.shape[2] > 3 else None,
        backdrop_alpha_houdini=round(float(at(ours, 1.8 + 0.65, -2.2)[3]), 4) if ours.shape[2] > 3 else None,
        colour_blender=at(reference, 1.8, -2.2, 0.5)[rgb].round(4).tolist(),
        colour_houdini=at(ours, 1.8, -2.2, 0.5)[rgb].round(4).tolist(),
        backdrop_houdini=at(ours, 1.8 + 0.65, -2.2)[rgb].round(4).tolist(),
    )
    check(
        "holdout: transparent black in both, the backdrop around it red and opaque",
        hole["alpha_houdini"] is not None
        and hole["alpha_houdini"] < 0.05
        and hole["alpha_blender"] < 0.05
        and hole["backdrop_alpha_houdini"] > 0.95
        and max(hole["colour_houdini"]) < 0.05
        and hole["backdrop_houdini"][0] > 0.8,
        hole,
    )


class Checks:
    def __init__(self):
        self.items = []

    def __call__(self, name, ok, message=""):
        self.items.append(dict(check=name, ok=bool(ok), message=str(message)[:1500]))
        print(("PASS " if ok else "FAIL ") + f"{name}: {str(message)[:300]}")
        return ok


def posix(path):
    return str(path).replace("\\", "/")


def drain():
    from cycles_houdini.blender import blend_catalogue

    for _ in range(600):
        if blend_catalogue.drain_catalogues():
            return True
        time.sleep(0.05)
    return False


def save_ppm(path, pixels):
    import numpy as np

    image = (np.clip(pixels, 0.0, 1.0) ** (1 / 2.2) * 255).astype(np.uint8)
    path.write_bytes(f"P6 {image.shape[1]} {image.shape[0]} 255\n".encode() + image.tobytes())


def blender_render(args, work, text=BLEND_SCRIPT, stem="parity", extra=()):
    from cycles_houdini.blender import blend_file_node as fn
    from cycles_houdini.blender.blend_worker import blender_environment

    executable = args.blender or fn.blender_executable()
    script = work / (stem + "_make_and_render.py")
    script.write_text(text, encoding="utf-8")
    blend, image = work / (stem + ".blend"), work / (stem + "_blender")
    completed = subprocess.run(
        [executable, "-b", "--factory-startup", "--python", str(script), "--", str(blend), str(image), str(SIZE)]
        + [str(value) for value in extra],
        timeout=300,
        capture_output=True,
        text=True,
        env=blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
    )
    if "BLENDER_RENDER_DONE" not in completed.stdout:
        raise RuntimeError("Blender render failed:\n" + completed.stdout[-3000:] + completed.stderr[-3000:])
    import numpy as np

    return executable, blend, np.load(str(image) + ".npy")


LAST_STATUS = {}  # the session status of the last houdini_render (source diagnostics)


def houdini_render(args, executable, blend, collection="Parity", channels=3, samples=8, settings=None):
    from cycles_houdini.blender import blend_import_dialog as dialog

    imported = time.time()
    options = dict(mode="link", save_location="files", structure="packed", materials="link", frames="current")
    source, chosen, kwargs = dialog.converter_arguments(posix(blend), [("collection", collection)], options, executable)
    backend = dialog.HoudiniBackend()
    payload = backend.prepare(source, chosen, kwargs, threading.Event(), lambda *a, **k: None)
    nodes = backend.build(payload)
    drain()
    node = nodes[0].node("blender_file")
    node.cook(force=True)
    if node.errors():
        raise RuntimeError(f"{node.path()}: {node.errors()}")
    print(f"LINK_IMPORT {time.time() - imported:.2f}s")
    return node, render_scene(args, channels, samples, settings)


def render_scene(args, channels=3, samples=8, settings=None):
    """Render every object in /obj through the Blender-matching camera, black world and ROP."""
    import numpy as np

    from cycles_houdini import native_scene, registry
    from cycles_houdini.device_selection import set_devices

    obj = hou.node("/obj")
    # Blender camera at (0, 0, 10) looking down -Z (up +Y) == Houdini (0, 10, 0) looking down -Y.
    cam = obj.createNode("cam", "parity_cam")
    cam.parmTuple("t").set((0.0, 10.0, 0.0))
    cam.parmTuple("r").set((-90.0, 0.0, 0.0))
    cam.parm("focal").set(50.0)
    cam.parm("aperture").set(36.0)
    cam.parm("resx").set(SIZE)
    cam.parm("resy").set(SIZE)
    # A black world like Blender's (without an environment light the constant world is grey).
    from cycles_houdini.scene import light_properties

    env = obj.createNode("envlight", "parity_black_world")
    light_properties.install(env)
    env.parm("light_intensity").set(0.0)
    rop = hou.node("/out").createNode("cycles_ipr", "parity_rop")
    set_devices(rop, args.device)
    rop.setParms(
        dict(
            source_mode="objects",
            objectroot="/obj",
            vobject="*",
            camera=cam.path(),
            lights="*",
            world_source="scene",
            resx=SIZE,
            resy=SIZE,
            samples=samples,
            ipr_samples=samples,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
        )
    )
    for name, value in (settings or {}).items():
        rop.parm(name).set(value)
    try:
        rendered = time.time()
        entry = registry.for_rop(rop, native_scene.configuration(rop))
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        LAST_STATUS.clear()
        LAST_STATUS.update(status)
        if status["error"]:
            raise RuntimeError(status["error"])
        counts = (
            "geometry_resources",
            "object_resources",
            "packed_instances",
            "geometry_conversions",
            "owned_geometry_bytes",
            "retained_mesh_source_bytes",
            "mesh_variant_bytes",
            "device_allocation_peak_bytes",
        )
        print("STATUS:" + json.dumps({key: status.get(key) for key in counts}))
        print(f"RENDER {time.time() - rendered:.2f}s")
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(SIZE, SIZE, plane["channels"])[..., :channels]
        pixels = pixels[::-1].copy()  # the frame buffer starts with the bottom row; top row first like Blender's
    finally:
        registry.close_all()
    return pixels


def process_memory():
    """(working set, peak working set) of this process in MiB (Windows), else (None, None)."""
    if os.name != "nt":
        return None, None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    kernel.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None, None
    return round(counters.WorkingSetSize / 2**20), round(counters.PeakWorkingSetSize / 2**20)


def points_performance(args, work):
    """Task H2: Link import and render time, record size and memory for one large point cloud."""
    started = time.time()
    executable, blend, _ = blender_render(
        args, work, POINTS_PERFORMANCE_SCRIPT, "points_performance", (args.points_performance,)
    )
    print(f"Blender scene {time.time() - started:.1f}s, .blend {blend.stat().st_size / 2**20:.1f} MiB")
    hou.hipFile.clear(suppress_save_prompt=True)
    (work / "scene").mkdir(exist_ok=True)
    hou.hipFile.save(str(work / "scene" / "points_performance.hip"))
    before = process_memory()
    started = time.time()
    houdini_render(args, executable, blend, "ManyPoints", samples=1)
    after = process_memory()
    numbers = dict(
        points=args.points_performance,
        link_and_render_seconds=round(time.time() - started, 2),
        working_set_mib_before=before[0],
        working_set_mib_after=after[0],
        peak_working_set_mib=after[1],
    )
    print("POINTS_PERFORMANCE:" + json.dumps(numbers))
    return 0


def main():
    import numpy as np

    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--blender", default=None)
    parser.add_argument("--threshold", type=float, default=0.02)
    parser.add_argument("--points-performance", type=int, default=0, help="time one cloud of N points only")
    args = parser.parse_args()
    check = Checks()
    work = (args.work or Path(tempfile.mkdtemp(prefix="cycles_blend_sync_parity_"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["CYCLES_HOUDINI_ASSET_CACHE"] = str(work / "cache")
    hou.putenv("CYCLES_HOUDINI_ASSET_CACHE", str(work / "cache"))
    print("Work folder:", work)
    if args.points_performance:
        return points_performance(args, work)
    started = time.time()
    try:
        executable, blend, reference = blender_render(args, work)
        print(f"Blender render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        (work / "scene").mkdir(exist_ok=True)
        hou.hipFile.save(str(work / "scene" / "parity.hip"))
        node, ours = houdini_render(args, executable, blend)
        print(f"Houdini render {time.time() - started:.1f}s")
        np.save(work / "houdini.npy", ours)
        save_ppm(work / "blender.ppm", reference)
        save_ppm(work / "houdini.ppm", ours)
        difference = np.abs(ours - reference)
        mean = float(difference.mean())
        numbers = dict(
            mean_abs_difference=mean,
            max_pixel=float(difference.max()),
            pixels_over_0_1=int((difference.max(axis=2) > 0.1).sum()),
            blender_mean=float(reference.mean()),
            houdini_mean=float(ours.mean()),
            images=posix(work),
        )
        print("NUMBERS:" + json.dumps(numbers))
        for label, image in (("blender", reference), ("houdini", ours)):
            # A coarse 12 x 12 view (mean RGB per cell, top row first) for reading the log.
            cells = image.reshape(12, SIZE // 12, 12, SIZE // 12, 3).mean(axis=(1, 3))
            print(label.upper() + "_GRID:")
            for row in cells:
                print("  " + " ".join("%.2f,%.2f,%.2f" % tuple(cell) for cell in row))
        check("Blender image not black", float(reference.mean()) > 0.02, numbers["blender_mean"])
        check("Houdini image not black", float(ours.mean()) > 0.02, numbers["houdini_mean"])
        check(f"mean absolute difference < {args.threshold}", mean < args.threshold, numbers)
        # Alignment: the best whole-pixel shift should be none (same camera and pixel centres).
        shifts = {}
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                moved = np.roll(np.roll(ours, dy, axis=0), dx, axis=1)
                shifts[f"{dy},{dx}"] = round(float(np.abs(moved - reference)[2:-2, 2:-2].mean()), 5)
        print("SHIFTS:" + json.dumps(shifts))
        check("no pixel shift between the images", min(shifts, key=shifts.get) == "0,0", shifts)
        # Regions (rows top to bottom = Blender +Y to -Y): planes on top, instances below.
        regions = dict(
            array_plane=(slice(22, 50), slice(4, 46)),
            custom_plane=(slice(22, 50), slice(52, 92)),
            cubes=(slice(55, 80), slice(18, 46)),
            rocks=(slice(55, 80), slice(50, 78)),
        )
        for name, (rows, columns) in regions.items():
            a, b = reference[rows, columns], ours[rows, columns]
            numbers = dict(
                mean_abs_difference=round(float(np.abs(a - b).mean()), 5),
                blender_mean=round(float(a.mean()), 5),
                houdini_mean=round(float(b.mean()), 5),
                lit_blender=int((a.max(axis=2) > 0.1).sum()),
                lit_houdini=int((b.max(axis=2) > 0.1).sum()),
            )
            check(
                f"{name}: visible in both, mean within 5%, mean absolute difference < {2 * args.threshold}",
                numbers["lit_blender"] > 20
                and numbers["lit_houdini"] > 20
                and abs(numbers["houdini_mean"] - numbers["blender_mean"]) <= 0.05 * numbers["blender_mean"]
                and numbers["mean_abs_difference"] < 2 * args.threshold,
                numbers,
            )
        # The flat side of the plane maps to 0.5 in blue (Generated z); the old rule gave 0.
        upper = slice(SIZE * 20 // 96, SIZE * 36 // 96)
        blue = ours[upper, :, 2][ours[upper].max(axis=2) > 0.1]
        check(
            "planes: Generated z of a flat side is 0.5",
            blue.size > 20 and abs(float(np.median(blue)) - 0.5) < 0.02,
            float(np.median(blue)) if blue.size else None,
        )
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("raised", False, traceback.format_exc()[-3000:])
    try:  # Task S2: Blender's per-object values (a second asset, same camera and settings).
        executable, blend, reference = blender_render(args, work, OBJECT_SCRIPT, "object_values")
        print(f"Blender object values render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "object_values.hip"))
        node, ours = houdini_render(args, executable, blend, "ObjectValues", 4)
        print(f"Houdini object values render {time.time() - started:.1f}s")
        np.save(work / "object_values_houdini.npy", ours)
        save_ppm(work / "object_values_blender.ppm", reference[..., :3])
        save_ppm(work / "object_values_houdini.ppm", ours[..., :3])
        for label, image in (("blender", reference), ("houdini", ours)):
            cells = image[..., :3].reshape(12, SIZE // 12, 12, SIZE // 12, 3).mean(axis=(1, 3))
            print("OBJECT_VALUES_" + label.upper() + "_GRID:")
            for row in cells:
                print("  " + " ".join("%.2f,%.2f,%.2f" % tuple(cell) for cell in row))
        object_values_checks(check, reference, ours, args.threshold)
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("object values raised", False, traceback.format_exc()[-3000:])
    try:  # Task S3: Pointiness and Random per Island (a third asset, same camera and settings).
        executable, blend, reference = blender_render(args, work, INTRINSIC_SCRIPT, "intrinsics")
        print(f"Blender intrinsics render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "intrinsics.hip"))
        node, ours = houdini_render(args, executable, blend, "Intrinsics")
        print(f"Houdini intrinsics render {time.time() - started:.1f}s")
        np.save(work / "intrinsics_houdini.npy", ours)
        save_ppm(work / "intrinsics_blender.ppm", reference[..., :3])
        save_ppm(work / "intrinsics_houdini.ppm", ours)
        for label, image in (("blender", reference), ("houdini", ours)):
            cells = image[..., :3].reshape(12, SIZE // 12, 12, SIZE // 12, 3).mean(axis=(1, 3))
            print("INTRINSICS_" + label.upper() + "_GRID:")
            for row in cells:
                print("  " + " ".join("%.2f" % cell[0] for cell in row))
        intrinsic_checks(check, reference[..., :3], ours, args.threshold)
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("intrinsics raised", False, traceback.format_exc()[-3000:])
    try:  # Task H1: hair (a fourth asset, same camera and settings).
        executable, blend, reference = blender_render(args, work, HAIR_SCRIPT, "hair")
        print(f"Blender hair render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "hair.hip"))
        # Blender's pixel filter (Blackman-Harris, 1.5 px): thin strands are mostly edge pixels.
        film = dict(pixel_filter="blackman_harris", filter_width=1.5)
        node, ours = houdini_render(args, executable, blend, "Hair", samples=32, settings=film)
        print(f"Houdini hair render {time.time() - started:.1f}s")
        np.save(work / "hair_houdini.npy", ours)
        save_ppm(work / "hair_blender.ppm", reference[..., :3])
        save_ppm(work / "hair_houdini.ppm", ours)
        for label, image in (("blender", reference), ("houdini", ours)):
            cells = image[..., :3].reshape(12, SIZE // 12, 12, SIZE // 12, 3).mean(axis=(1, 3))
            print("HAIR_" + label.upper() + "_GRID:")
            for row in cells:
                print("  " + " ".join("%.2f,%.2f,%.2f" % tuple(cell) for cell in row))
        hair_checks(check, reference[..., :3], ours, args.threshold)
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("hair raised", False, traceback.format_exc()[-3000:])
    try:  # Task H2: point clouds (a fifth asset, same camera and settings as the hair asset).
        executable, blend, reference = blender_render(args, work, POINTS_SCRIPT, "points")
        print(f"Blender points render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "points.hip"))
        film = dict(pixel_filter="blackman_harris", filter_width=1.5)  # Blender's pixel filter
        node, ours = houdini_render(args, executable, blend, "Points", samples=32, settings=film)
        print(f"Houdini points render {time.time() - started:.1f}s")
        np.save(work / "points_houdini.npy", ours)
        save_ppm(work / "points_blender.ppm", reference[..., :3])
        save_ppm(work / "points_houdini.ppm", ours)
        for label, image in (("blender", reference), ("houdini", ours)):
            cells = image[..., :3].reshape(12, SIZE // 12, 12, SIZE // 12, 3).mean(axis=(1, 3))
            print("POINTS_" + label.upper() + "_GRID:")
            for row in cells:
                print("  " + " ".join("%.2f,%.2f,%.2f" % tuple(cell) for cell in row))
        points_checks(check, reference[..., :3], ours, args.threshold)
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("points raised", False, traceback.format_exc()[-3000:])
    try:  # Task L1: volume shaders on meshes (a sixth asset, same camera).
        executable, blend, reference = blender_render(args, work, MEDIA_SCRIPT, "media")
        print(f"Blender media render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "media.hip"))
        # Blender's render settings for this comparison: its pixel filter (Blackman-Harris 1.5 px), its
        # final-render sampling pattern (Automatic = pure blue noise in a background render; the
        # ROP's default is Tabulated Sobol), no adaptive sampling (this session is interactive, and
        # the ROP's IPR Adaptive Sampling is on by default), and Blender's default light paths
        # (12 total, 4 diffuse, 4 glossy, 12 transmission, 0 volume, 8 transparent bounces; light
        # threshold 0.01), so the noise is comparable and not only the mean.
        media_film = dict(
            pixel_filter="blackman_harris",
            filter_width=1.5,
            sampling_pattern="blue_noise",
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            max_bounce=12,
            diffuse_bounce=4,
            glossy_bounce=4,
            transmission_bounce=12,
            volume_bounce=0,
            transparent_max_bounce=8,
            light_threshold=0.01,
        )
        node, ours = houdini_render(args, executable, blend, "Media", samples=64, settings=media_film)
        print(f"Houdini media render {time.time() - started:.1f}s")
        np.save(work / "media_houdini.npy", ours)
        save_ppm(work / "media_blender.ppm", reference)
        save_ppm(work / "media_houdini.ppm", ours)
        for label, image in (("blender", reference), ("houdini", ours)):
            cells = image.reshape(12, SIZE // 12, 12, SIZE // 12, 3).mean(axis=(1, 3))
            print("MEDIA_" + label.upper() + "_GRID:")
            for row in cells:
                print("  " + " ".join("%.2f,%.2f,%.2f" % tuple(cell) for cell in row))
        media_checks(check, reference, ours, args.threshold, LAST_STATUS.get("source_diagnostics") or [])
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("media raised", False, traceback.format_exc()[-3000:])
    try:  # Task L1: the same cubes as native Houdini boxes with native Cycles materials.
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "native_media.hip"))
        native_media_scene()
        ours = render_scene(args, samples=64, settings=media_film)
        print(f"Houdini native media render {time.time() - started:.1f}s")
        np.save(work / "native_media_houdini.npy", ours)
        save_ppm(work / "native_media_houdini.ppm", ours)
        native_media_checks(check, reference, ours, args.threshold, LAST_STATUS.get("source_diagnostics") or [])
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("native media raised", False, traceback.format_exc()[-3000:])
    try:  # Task L1: per-curve and integer radii (a seventh asset, same camera as the hair asset).
        executable, blend, reference = blender_render(args, work, RADII_SCRIPT, "radii")
        print(f"Blender radii render {time.time() - started:.1f}s")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.hipFile.save(str(work / "scene" / "radii.hip"))
        film = dict(pixel_filter="blackman_harris", filter_width=1.5)  # Blender's pixel filter
        node, ours = houdini_render(args, executable, blend, "Radii", samples=32, settings=film)
        print(f"Houdini radii render {time.time() - started:.1f}s")
        np.save(work / "radii_houdini.npy", ours)
        save_ppm(work / "radii_blender.ppm", reference)
        save_ppm(work / "radii_houdini.ppm", ours)
        radii_checks(check, reference, ours, args.threshold)
    except Exception:  # noqa: BLE001 - reported as a failed check
        check("radii raised", False, traceback.format_exc()[-3000:])
    failed = [item for item in check.items if not item["ok"]]
    summary = dict(
        result="failed" if failed else "passed",
        work=posix(work),
        seconds=round(time.time() - started, 1),
        houdini=hou.applicationVersionString(),
        passed=len(check.items) - len(failed),
        failed=len(failed),
        checks=check.items,
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
    try:  # stop Blender workers before the hard exit (os._exit skips atexit)
        from cycles_houdini.blender import blend_worker

        blend_worker.shutdown_all()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    sys.stdout.flush()
    # Houdini's own shutdown, like the other smokes: os._exit crashed in exit handlers
    # (a shiboken atexit callback, then the PDG data layer) with exit code 139 after "passed".
    hou.exit(code, suppress_save_prompt=True)
    os._exit(code)
