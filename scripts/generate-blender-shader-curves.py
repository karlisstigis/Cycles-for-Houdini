# SPDX-License-Identifier: GPL-3.0-or-later
"""Run with pinned Blender --background --factory-startup --python-exit-code 1."""

import bpy, hashlib, json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
assert bpy.app.build_hash.decode() == "fbe6228777e7", "Use the pinned Blender reference build"
specs = []
identity = lambda lo=0.0, hi=1.0: [(lo, lo, "AUTO"), (hi, hi, "AUTO")]
for kind, node_type, channels in [
    ("float", "ShaderNodeFloatCurve", 1),
    ("rgb", "ShaderNodeRGBCurve", 4),
    ("vector", "ShaderNodeVectorCurve", 3),
]:
    lo = -1.0 if kind == "vector" else 0.0
    for mode in ("AUTO", "AUTO_CLAMPED", "VECTOR"):
        points = [(lo, 0.15, mode), (0.13, 0.85, mode), (0.37, -0.25, mode), (0.82, 1.3, mode), (1.0, 0.7, mode)]
        configs = [points] + [identity(lo, 1.0) for _ in range(channels - 1)]
        if kind == "rgb":
            configs[3] = [(0.0, 0.0, mode), (0.3, 0.57, mode), (0.8, 0.73, mode), (1.0, 1.0, mode)]
        if kind == "vector":
            configs[1] = [(-1.0, 0.5, mode), (-0.2, -0.5, mode), (1.0, 1.2, mode)]
        for extension in ("EXTRAPOLATED", "HORIZONTAL"):
            specs.append(
                dict(
                    id=kind + "_" + mode.lower() + "_" + extension.lower(),
                    kind=kind,
                    node_type=node_type,
                    extension=extension,
                    domain=[lo, 1.0],
                    channels=configs,
                )
            )
    specs.append(
        dict(
            id=kind + "_identity",
            kind=kind,
            node_type=node_type,
            extension="EXTRAPOLATED",
            domain=[lo, 1.0],
            channels=[identity(lo, 1.0) for _ in range(channels)],
        )
    )
    configs = [[(0.2, -0.4, "AUTO"), (0.205, 2.0, "VECTOR"), (0.73, 0.1, "AUTO_CLAMPED"), (0.8, 1.4, "AUTO")]] + [
        identity(lo, 1.0) for _ in range(channels - 1)
    ]
    specs.append(
        dict(
            id=kind + "_nonuniform",
            kind=kind,
            node_type=node_type,
            extension="EXTRAPOLATED",
            domain=[lo, 1.0],
            channels=configs,
        )
    )
    configs = [[(-2.0, -1.0, "VECTOR"), (-0.1, 0.75, "AUTO_CLAMPED"), (1.2, 2.1, "AUTO"), (3.0, 4.0, "VECTOR")]] + [
        identity(lo, 1.0) for _ in range(channels - 1)
    ]
    specs.append(
        dict(
            id=kind + "_hdr_domain",
            kind=kind,
            node_type=node_type,
            extension="EXTRAPOLATED",
            domain=[lo, 1.0],
            channels=configs,
        )
    )

tree = bpy.data.materials.new("Curve facts").node_tree
if tree is None:
    material = bpy.data.materials[-1]
    material.use_nodes = True
    tree = material.node_tree
rows = []
for spec in specs:
    node = tree.nodes.new(spec["node_type"])
    mapping = node.mapping
    mapping.initialize()
    mapping.use_clip = False
    mapping.extend = spec["extension"]
    mapping.clip_min_x = spec["domain"][0]
    mapping.clip_max_x = spec["domain"][1]
    for curve, points in zip(mapping.curves, spec["channels"]):
        curve.points[0].location = points[0][:2]
        curve.points[-1].location = points[-1][:2]
        curve.points[0].handle_type = points[0][2]
        curve.points[-1].handle_type = points[-1][2]
        for x, y, handle in points[1:-1]:
            curve.points.new(x, y).handle_type = handle
    mapping.update()
    controls = [[(float(p.location.x), float(p.location.y), p.handle_type) for p in c.points] for c in mapping.curves]
    minimum = min(c[0][0] for c in controls)
    maximum = max(c[-1][0] for c in controls)
    table = []
    for i in range(257):
        x = minimum + i / 256 * (maximum - minimum)
        common = mapping.evaluate(mapping.curves[3], x) if spec["kind"] == "rgb" else x
        table.append([mapping.evaluate(mapping.curves[0 if spec["kind"] == "float" else j], common) for j in range(3)])
    rows.append(
        dict(
            **{k: v for k, v in spec.items() if k != "channels"},
            channels=controls,
            minimum=minimum,
            maximum=maximum,
            table=table,
        )
    )
    tree.nodes.remove(node)
references = [
    {"path": "colortools.cc", "sha256": "d264c003b85203afaa33d0508774c5426a51ebdc7afbeec0a7a9ffc7ae2e2330"},
    {"path": "curve.cc", "sha256": "c53714fca04c0a086ba6930c96a4cfcef855ff3f89721784307638da2483a983"},
    {"path": "cycles-shader.cpp", "sha256": "1d5d479326dd6bed43642fde4488c2e5fc6bf6d94002e5c2d7fda866f287f9a0"},
    {"path": "cycles-util.h", "sha256": "651d8f99c71ba143ab9f41190a9b03925201efa4ee8cee2814078c52cdd4790d"},
    {"path": "DNA_color_types.h", "sha256": "a6ee930f701cf512186e6b7275c561620174250160b1302a5cd31dd9d0bf0d7f"},
]
result = dict(
    blender_version=bpy.app.version_string,
    blender_commit=bpy.app.build_hash.decode(),
    scope="Independent public CurveMapping.evaluate values with clip disabled, combined-first RGB composition and the pinned Cycles exporter 257-sample domain. Reference only; no Blender runtime dependency.",
    source_references=references,
    cases=rows,
)
path = root / "tests/fixtures/blender-shader-curves.json"
path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(dict(result="passed", cases=len(rows), reference=str(path))))
