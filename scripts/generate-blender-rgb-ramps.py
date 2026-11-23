# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent Blender RGB interpolation/alpha reference through its public API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_table_reference import sampled_rgba, write_tables

import bpy, json, struct
from pathlib import Path

assert bpy.app.build_hash.decode() == "fbe6228777e7"
root = Path(__file__).resolve().parents[1]
f32 = lambda x: struct.unpack("f", struct.pack("f", x))[0]
palettes = [
    ("default", [0, 1], [(0, 0, 0, 0), (1, 1, 1, 1)]),
    (
        "nonuniform",
        [0.06, 0.19, 0.54, 0.93],
        [(0.8, 0.1, 0.3, 0.1), (0.1, 0.9, 0.2, 0.8), (0.3, 0.2, 0.85, 0.25), (0.65, 0.5, 0.2, 0.9)],
    ),
    ("overshoot", [0.1, 0.2, 0.6, 0.9], [(0, 1, 0, 0), (1, 0, 1, 1), (1, 0, 1, 1), (0, 1, 0, 0)]),
    (
        "hdr",
        [0.05, 0.35, 0.75, 0.98],
        [(3, 0.2, 0.1, 0.2), (0.1, 2, 0.4, 0.8), (0.7, 0.1, 1.6, 0.3), (0.6, 0.6, 0.6, 0.9)],
    ),
    ("duplicate_first", [0.1, 0.1, 0.9], [(1, 0, 0, 0.15), (0, 1, 0, 0.7), (0, 0, 1, 0.9)]),
    ("duplicate_last", [0.1, 0.9, 0.9], [(1, 0, 0, 0.1), (0, 1, 0, 0.4), (0, 0, 1, 0.9)]),
    ("duplicate_middle", [0, 0.4, 0.4, 1], [(1, 0, 0, 0), (0, 1, 0, 0.25), (0, 0, 1, 0.75), (1, 1, 0, 1)]),
    ("duplicate_zero", [0, 0, 0.7, 1], [(1, 0, 0, 0.1), (0, 1, 0, 0.8), (0, 0, 1, 0.3), (1, 1, 0, 0.9)]),
    ("duplicate_one", [0, 0.4, 1, 1], [(1, 0, 0, 0.1), (0, 1, 0, 0.8), (0, 0, 1, 0.3), (1, 1, 0, 0.9)]),
    ("all_zero", [0, 0, 0], [(1, 0, 0, 0.1), (0, 1, 0, 0.4), (0, 0, 1, 0.9)]),
    ("all_one", [1, 1, 1], [(1, 0, 0, 0.1), (0, 1, 0, 0.4), (0, 0, 1, 0.9)]),
    ("single", [0.45], [(0.2, 0.4, 0.7, 0.35)]),
    (
        "maximum",
        [i / 31 for i in range(32)],
        [(0.2 + (i % 3) * 0.3, 0.1 + (i % 5) * 0.1, 0.3 + (i % 4) * 0.1, (i % 7) / 6) for i in range(32)],
    ),
]
positions = {f32(i / 64) for i in range(65)} | {f32(0.425), -0.25, 1.25}
for _, keys, _ in palettes:
    for v in keys:
        x = f32(v)
        positions.add(x)
        bits = struct.unpack("I", struct.pack("f", x))[0]
        if 0 < x < 1:
            positions.add(struct.unpack("f", struct.pack("I", bits - 1))[0])
            positions.add(struct.unpack("f", struct.pack("I", bits + 1))[0])
positions = sorted(positions)
mat = bpy.data.materials.new("RGB interpolation reference")
mat.use_nodes = True
ramp = mat.node_tree.nodes.new("ShaderNodeValToRGB").color_ramp
rows = []
for method in ("LINEAR", "CONSTANT", "EASE", "B_SPLINE", "CARDINAL"):
    for name, keys, colors in palettes:
        while len(ramp.elements) > 1:
            ramp.elements.remove(ramp.elements[-1])
        ramp.elements[0].position = 0
        for i in range(1, len(keys)):
            ramp.elements.new(i / max(1, len(keys) - 1))
        for element, key, rgba in zip(list(ramp.elements), keys, colors):
            element.position = key
            element.color = rgba
        ramp.color_mode = "RGB"
        ramp.interpolation = method
        controls = [
            dict(position=float(e.position), color=list(e.color)[:3], alpha=float(e.color[3])) for e in ramp.elements
        ]
        expected = [list(ramp.evaluate(x)) for x in positions]
        baked = sampled_rgba(ramp, positions)
        rows.append(
            dict(
                id=method.lower() + "_" + name,
                mode="RGB",
                hue="NEAR",
                interpolation=method,
                controls=controls,
                factors=positions,
                colors=[v[:3] for v in expected],
                table_colors=[v[:3] for v in baked],
                table_alphas=[v[3] for v in baked],
                alphas=[v[3] for v in expected],
            )
        )
path = root / "tests/fixtures/blender-rgb-ramps.json"
write_tables(rows, path)
path.write_text(
    json.dumps(
        dict(blender_version=bpy.app.version_string, blender_commit=bpy.app.build_hash.decode(), cases=rows),
        indent=2,
        allow_nan=False,
    )
    + "\n"
)
print(json.dumps(dict(result="passed", cases=len(rows), factors=len(positions), path=str(path))))
