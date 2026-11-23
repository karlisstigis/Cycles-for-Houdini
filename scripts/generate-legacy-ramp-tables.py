# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake the frozen migration fixture with pinned public Blender evaluators.

Native Houdini bases and LAB are evaluated independently by hou.Ramp in the
test. This companion covers Blender RGB/hue and linear Alpha sources only.
The retired exact-evaluation fixture is never rewritten.
"""

import hashlib
import json
from pathlib import Path
import struct
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_table_reference import sampled_rgba

assert bpy.app.build_hash.decode() == "02f0899d78e3"
root = Path(__file__).resolve().parents[1]
path = root / "tests/fixtures/legacy-rgba-color-ramps.json"
reference = json.loads(path.read_text())
material = bpy.data.materials.new("Legacy migration stock table reference")
material.use_nodes = True
ramp = material.node_tree.nodes.new("ShaderNodeValToRGB").color_ramp
f32 = lambda bits: struct.unpack("f", struct.pack("i", bits))[0]
cases = {}
for name, frames in reference["expected"].items():
    cases[name] = {}
    for frame, expected in frames.items():
        row = {}
        for output, source in zip(("color", "alpha"), expected["sources"]):
            if source[1] == 1:
                continue  # Public hou.Ramp owns the native reference.
            assert source[1] in (2, 4, 5), source
            count = source[2]
            while len(ramp.elements) > 1:
                ramp.elements.remove(ramp.elements[-1])
            ramp.elements[0].position = 0
            for i in range(1, count):
                ramp.elements.new(i / max(1, count - 1))
            for i, element in enumerate(list(ramp.elements)):
                start = 8 + 5 * i
                element.position = f32(source[start + 1])
                element.color = (*map(f32, source[start + 2 : start + 5]), 1.0)
            ramp.color_mode = {0: "RGB", 1: "HSV", 3: "HSL"}[source[3]]
            ramp.hue_interpolation = ("NEAR", "FAR", "CW", "CCW")[source[5]] if source[1] == 2 else "NEAR"
            ramp.interpolation = (
                ("CONSTANT", "LINEAR", "EASE", "B_SPLINE", "CARDINAL")[source[5]] if source[1] == 5 else "LINEAR"
            )
            values = sampled_rgba(ramp, reference["factors"])
            row[output] = [value[:3] if output == "color" else value[0] for value in values]
        cases[name][frame] = row
result = dict(
    scope="Independent pinned Blender public ColorRamp.evaluate samples baked at 257 entries, then stock lookup",
    blender_version=bpy.app.version_string,
    blender_commit=bpy.app.build_hash.decode(),
    legacy_fixture_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    cases=cases,
)
path.with_name("legacy-rgba-color-ramps-tables.json").write_text(json.dumps(result, separators=(",", ":")) + "\n")
