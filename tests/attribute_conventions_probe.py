# SPDX-License-Identifier: Apache-2.0
"""Verify the public host's point-radius and curve-width conventions."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import hou

geo = hou.node("/obj").createNode("geo", "attribute_conventions", run_init_scripts=False)
points = geo.createNode("python", "points")
points.parm("python").set("""import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Point, 'pscale', 0.25)
for y in (-0.5, 0.5): g.createPoint().setPosition((0, y, 0))
""")
curve = geo.createNode("python", "curve")
curve.parm("python").set("""import hou
g = hou.pwd().geometry(); g.addAttrib(hou.attribType.Point, 'width', 0.2)
p = g.createPolygon(is_closed=False)
for y in (-0.5, 0.5):
    point = g.createPoint(); point.setPosition((0, y, 0)); p.addVertex(point)
""")
import_node = hou.node("/stage").createNode("sopimport")
evidence = []
for node, expected_type, expected_width in ((points, "Points", 0.5), (curve, "BasisCurves", 0.2)):
    import_node.parm("soppath").set(node.path())
    rendered = [primitive for primitive in import_node.stage().Traverse() if primitive.GetTypeName() == expected_type]
    assert len(rendered) == 1
    widths = list(rendered[0].GetAttribute("widths").Get())
    assert len(widths) == 2 and max(abs(width - expected_width) for width in widths) < 1e-7, widths
    evidence.append(dict(source=node.name(), usd_type=expected_type, widths=widths))
destination = artifacts_root() / "stage2/attribute-conventions.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(
    json.dumps(
        dict(
            result="passed",
            houdini=hou.applicationVersionString(),
            evidence=evidence,
            convention="Point pscale is radius; open curve width is full width",
        ),
        indent=2,
    ),
    encoding="utf-8",
)
print(destination)
