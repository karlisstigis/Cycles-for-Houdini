# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent public Blender RGBA evaluation; run in the pinned Blender build."""

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
    ("defaults", [0, 1], [(0, 0, 0), (1, 1, 1)], [0, 1]),
    (
        "nonuniform",
        [0.05, 0.2, 0.6, 0.95],
        [(0.8, 0.1, 0.2), (0.2, 0.8, 0.3), (0.2, 0.3, 0.8), (0.7, 0.7, 0.7)],
        [0.1, 0.8, 0.25, 0.9],
    ),
    ("duplicate_first", [0.1, 0.1, 0.9], [(1, 0, 0), (0, 1, 0), (0, 0, 1)], [0.15, 0.7, 0.9]),
    ("duplicate_interior", [0, 0.4, 0.4, 1], [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)], [0, 0.25, 0.75, 1]),
    ("duplicate_last", [0.1, 0.9, 0.9], [(1, 0, 0), (0, 1, 0), (0, 0, 1)], [0.1, 0.4, 0.9]),
    ("single", [0.45], [(0.2, 0.4, 0.7)], [0.35]),
    (
        "maximum_keys",
        [i / 31 for i in range(32)],
        [(0.2 + (i % 3) * 0.3, 0.1 + (i % 5) * 0.1, 0.3 + (i % 4) * 0.1) for i in range(32)],
        [(i % 7) / 6 for i in range(32)],
    ),
]
positions = {f32(i / 32) for i in range(33)} | {f32(0.425)}
for _, keys, _, _ in palettes:
    for value in keys:
        x = f32(value)
        positions.add(x)
        bits = struct.unpack("I", struct.pack("f", x))[0]
        if x > 0:
            positions.add(struct.unpack("f", struct.pack("I", bits - 1))[0])
        if 0 < x < 1:
            positions.add(struct.unpack("f", struct.pack("I", bits + 1))[0])
positions = sorted(positions)
mat = bpy.data.materials.new("RGBA public reference")
mat.use_nodes = True
ramp = mat.node_tree.nodes.new("ShaderNodeValToRGB").color_ramp
defaults = [dict(position=e.position, rgba=list(e.color)) for e in ramp.elements]
rows = []
for mode in ("RGB", "HSV", "HSL"):
    for direction in ("NEAR",) if mode == "RGB" else ("NEAR", "FAR", "CW", "CCW"):
        for name, keys, colors, alpha in palettes:
            if mode == "RGB" and "duplicate" in name:
                continue  # Native RGB duplicate stops intentionally recover.
            while len(ramp.elements) > 1:
                ramp.elements.remove(ramp.elements[-1])
            ramp.elements[0].position = 0
            for i in range(1, len(keys)):
                ramp.elements.new(i / max(1, len(keys) - 1))
            for e, p, c, a in zip(list(ramp.elements), keys, colors, alpha):
                e.position = p
                e.color = (*c, a)
            ramp.color_mode = mode
            ramp.hue_interpolation = direction
            ramp.interpolation = "LINEAR"
            controls = [
                dict(position=float(e.position), color=list(e.color)[:3], alpha=float(e.color[3]))
                for e in ramp.elements
            ]
            expected = [list(ramp.evaluate(x)) for x in positions]
            if mode != "RGB":
                for interpolation in ("CONSTANT", "EASE", "CARDINAL", "B_SPLINE"):
                    ramp.interpolation = interpolation
                    assert [list(ramp.evaluate(x)) for x in positions] == expected
            baked = sampled_rgba(ramp, positions)
            rows.append(
                dict(
                    id=mode.lower() + "_" + direction.lower() + "_" + name,
                    mode=mode,
                    hue=direction,
                    controls=controls,
                    factors=positions,
                    colors=[e[:3] for e in expected],
                    table_colors=[v[:3] for v in baked],
                    table_alphas=[v[3] for v in baked],
                    alphas=[e[3] for e in expected],
                )
            )
path = root / "tests/fixtures/blender-rgba-ramps.json"
write_tables(rows, path)
path.write_text(
    json.dumps(
        dict(
            blender_version=bpy.app.version_string,
            blender_commit=bpy.app.build_hash.decode(),
            scope="Independent ColorRamp.evaluate RGBA references; hue interpolation never interpolates alpha as hue.",
            defaults=defaults,
            cases=rows,
        ),
        indent=2,
        allow_nan=False,
    )
    + "\n"
)
print(json.dumps(dict(result="passed", cases=len(rows), reference=str(path))))
