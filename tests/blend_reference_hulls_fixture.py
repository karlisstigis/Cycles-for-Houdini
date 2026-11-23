# SPDX-License-Identifier: Apache-2.0
"""Owned, separated dense meshes for visual Blender Reference hull acceptance."""

import argparse
from pathlib import Path
import sys

import bpy

parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
args.out.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
collection = bpy.data.collections.new("Hull Collection")
bpy.context.scene.collection.children.link(collection)
for name, x, scale in (("Hull Left", -2, (0.8, 0.65, 1.25)), ("Hull Right", 2, (1.0, 0.65, 0.75))):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1, location=(x, 0, 0))
    obj = bpy.context.object
    obj.name = name
    for vertex in obj.data.vertices:
        for axis in range(3):
            vertex.co[axis] *= scale[axis]
    for parent in list(obj.users_collection):
        parent.objects.unlink(obj)
    collection.objects.link(obj)
bpy.context.scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=str(args.out.resolve()), relative_remap=False)
