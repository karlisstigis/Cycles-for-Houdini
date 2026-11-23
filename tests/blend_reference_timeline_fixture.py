# SPDX-License-Identifier: Apache-2.0
"""Owned animated Blender object for source-frame versus Houdini-frame checks."""

import argparse
from pathlib import Path
import sys

import bpy

parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
args.out.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1)
obj = bpy.context.object
obj.name = "Hull Left"
for vertex in obj.data.vertices:
    vertex.co.x *= 0.55
    vertex.co.y *= 0.45
    vertex.co.z *= 0.8
obj.shape_key_add(name="Basis")
shape = obj.shape_key_add(name="Source Pose")
for vertex in shape.data:
    vertex.co.z *= 1.75
    vertex.co.x += max(0, vertex.co.z) * 0.35
for frame, value in ((1, 0), (9, 1)):
    shape.value = value
    shape.keyframe_insert(data_path="value", frame=frame)
bpy.context.scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=str(args.out.resolve()), relative_remap=False)
