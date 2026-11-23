# SPDX-License-Identifier: GPL-3.0-or-later
"""Run with pinned Blender --background --factory-startup --python-exit-code 1."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_table_reference import sampled_rgba, write_tables

import bpy, colorsys, json, math, struct
from pathlib import Path

assert bpy.app.build_hash.decode() == "fbe6228777e7", "Use the pinned Blender reference build"
root = Path(__file__).resolve().parents[1]
f32 = lambda x: struct.unpack("f", struct.pack("f", x))[0]


def neighbor(x, direction):
    n = struct.unpack("I", struct.pack("f", x))[0]
    return struct.unpack("f", struct.pack("I", n + direction))[0]


palettes = [
    ("red_blue", [0, 1], [(1, 0, 0), (0, 0, 1)]),
    ("blue_red", [0, 1], [(0, 0, 1), (1, 0, 0)]),
    ("half_turn", [0, 1], [(1, 0, 0), (0, 1, 1)]),
    ("half_turn_offset", [0, 1], [(1, 1, 0), (0, 0, 1)]),
    ("hue_seam", [0.08, 0.92], [(1, 0.05, 0.2), (1, 0.2, 0.05)]),
    ("equal_hue", [0, 1], [(0.8, 0, 0), (0.4, 0, 0)]),
    ("achromatic", [0, 0.3, 0.7, 1], [(0, 0, 0), (0.35, 0.35, 0.35), (0, 0.6, 0.8), (1, 1, 1)]),
    (
        "nonuniform",
        [0.05, 0.16, 0.37, 0.81, 0.97],
        [(0.8, 0.1, 0.4), (0.2, 0.7, 0.3), (0.3, 0.3, 0.3), (0.4, 0.2, 0.9), (0.9, 0.6, 0.1)],
    ),
    ("duplicate_interior", [0, 0.4, 0.4, 1], [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)]),
    ("duplicate_first", [0.1, 0.1, 0.9], [(1, 0, 0), (0, 1, 0), (0, 0, 1)]),
    ("duplicate_last", [0.1, 0.9, 0.9], [(1, 0, 0), (0, 1, 0), (0, 0, 1)]),
    ("single", [0.45], [(0.2, 0.4, 0.7)]),
    (
        "maximum_keys",
        [i / 31 for i in range(32)],
        [colorsys.hsv_to_rgb((i * 0.381966) % 1, 0.3 + 0.7 * (i % 3) / 2, 0.4 + 0.6 * (i % 5) / 4) for i in range(32)],
    ),
    ("hdr", [0, 0.31, 0.74, 1], [(3, 0.2, 0.1), (0.1, 2, 0.4), (0.7, 0.1, 1.6), (0.6, 0.6, 0.6)]),
]
material = bpy.data.materials.new("Hue ramp numerical reference")
material.use_nodes = True
node = material.node_tree.nodes.new("ShaderNodeValToRGB")
ramp = node.color_ramp
positions = {f32(i / 32) for i in range(33)} | {f32(0.425)}
for _, keys, _ in palettes:
    for value in keys:
        x = f32(value)
        positions.add(x)
        if x > 0:
            positions.add(neighbor(x, -1))
        if 0 < x < 1:
            positions.add(neighbor(x, 1))
positions = sorted(positions)
rows = []
for mode in ("HSV", "HSL"):
    for direction in ("NEAR", "FAR", "CW", "CCW"):
        for name, keys, colors in palettes:
            while len(ramp.elements) > 1:
                ramp.elements.remove(ramp.elements[-1])
            # Build away from coincident positions first, then set all final positions
            # in index order. RNA insertion sorts keys and must not reorder duplicates.
            ramp.elements[0].position = 0
            for i in range(1, len(keys)):
                ramp.elements.new(i / max(1, len(keys) - 1))
            elements = list(ramp.elements)
            for element, x, color in zip(elements, keys, colors):
                element.position = x
                element.color = (*color, 1.0)
            ramp.color_mode = mode
            ramp.hue_interpolation = direction
            ramp.interpolation = "LINEAR"
            controls = [dict(position=float(e.position), color=list(e.color)[:3]) for e in ramp.elements]
            assert [e["position"] for e in controls] == list(map(f32, keys))
            assert [e["color"] for e in controls] == [[f32(c) for c in color] for color in colors]
            expected = [list(ramp.evaluate(x))[:3] for x in positions]
            for interpolation in ("EASE", "CARDINAL", "B_SPLINE", "CONSTANT"):
                ramp.interpolation = interpolation
                assert [list(ramp.evaluate(x))[:3] for x in positions] == expected, (
                    mode,
                    direction,
                    name,
                    interpolation,
                )
            baked = sampled_rgba(ramp, positions)
            rows.append(
                dict(
                    id=mode.lower() + "_" + direction.lower() + "_" + name,
                    mode=mode,
                    hue=direction,
                    controls=controls,
                    factors=positions,
                    colors=expected,
                    table_colors=[v[:3] for v in baked],
                )
            )
result = dict(
    blender_version=bpy.app.version_string,
    blender_commit=bpy.app.build_hash.decode(),
    scope="Independent public ColorRamp.evaluate RGB values. HSV/HSL force linear interpolation; all four hue directions, coincident stops, achromatic/HDR inputs and 1..32 stops. No Blender runtime dependency.",
    defaults=dict(color_mode="RGB", hue_interpolation="NEAR", interpolation="LINEAR"),
    cases=rows,
)
path = root / "tests/fixtures/blender-hue-ramps.json"
write_tables(rows, path)
path.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
print(json.dumps(dict(result="passed", cases=len(rows), reference=str(path))))
