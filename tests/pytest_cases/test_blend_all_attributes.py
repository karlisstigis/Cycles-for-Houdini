# SPDX-License-Identifier: Apache-2.0
"""Convert & Append carries every attribute kind of Blender 5.3 (the artist's "all attributes"
rule), against a real Blender (bpy module).

tests/blend_all_attributes_fixture.py builds a .blend with one attribute per domain and data
type on a mesh, a Curves object and a point cloud (plus vertex groups, UV maps and colour
attributes with odd names, names that collide with Houdini's, material slots and an
instance-domain attribute). The exporter and blend_convert turn it into Houdini geometry; every
attribute is compared with the table of tests/blend_all_attributes_fixture.expectations
(owner, type, size, name, type info, every value). The same table is checked in Houdini by
tests/houdini_blend_converted_attributes_smoke.py.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.blender, pytest.mark.cpu]

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, sys, tempfile
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender", sys.argv[2]]
import bpy
import numpy as np
import blend_all_attributes_fixture as fixture
from cycles_blender import blend_export
from cycles_houdini.blender import blend_convert

out = sys.argv[3]
expected = fixture.build(bpy)
path = out + "/all_attributes.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)
flags = dict(
    vertex_groups=True, curves=True, pointclouds=True, object_tree=True, native_subdivision=False,
    nested_instances=True, material_export="names",
)
with tempfile.TemporaryDirectory() as textures:
    [(manifest, arrays)] = blend_export.evaluate_many(
        [dict(source=path, selector_type="COLLECTION", selector="AllAttributes", frame=1.0, **flags)], textures
    )
names = {record["key"]: record["name"] for record in manifest["materials"] if not record.get("default")}
report = blend_convert.Report()
frame = blend_convert.assemble(manifest, arrays, "hierarchy", names, report, {})
objects = {}
for entry in frame["objects"]:
    geo = entry["geo"]
    if geo is None:
        continue
    attribs = {}
    for name, attrib in geo.attribs.items():
        values = list(attrib.values) if attrib.kind == "string" else np.asarray(attrib.values).tolist()
        attribs[name] = [attrib.owner, attrib.kind, attrib.width, attrib.info, values]
    objects[entry["name"]] = dict(
        attribs=attribs,
        prim_groups={k: sorted(int(i) for i in v) for k, v in geo.prim_groups.items()},
        edge_groups={k: sorted(sorted(int(i) for i in pair) for pair in np.asarray(v).reshape(-1, 2)) for k, v in geo.edge_groups.items()},
    )
print("RESULT:" + json.dumps(dict(
    expected=expected, objects=objects, lines=blend_convert.attribute_lines(report), report=report.as_dict(),
), ensure_ascii=False))
"""


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_all_attributes")
    script = folder / "convert_all.py"
    script.write_text(SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT / "houdini/python3.13libs"), str(ROOT / "tests"), str(folder)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


def _fixture():
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        import blend_all_attributes_fixture as fixture
    finally:
        sys.path.remove(str(ROOT / "tests"))
    return fixture


_QUALIFIER = {"quaternion": "Quaternion", "matrix": "Matrix"}


def test_every_attribute_arrives(converted):
    fixture = _fixture()
    rows, absent, groups, _ = fixture.expectations(converted["expected"])
    objects = converted["objects"]

    def lookup(obj, name):
        found = objects.get(obj, {}).get("attribs", {}).get(name)
        if found is None:
            return None
        owner, kind, width, info, values = found
        values = [[value] for value in values] if kind == "string" else values
        return owner, kind, width, _QUALIFIER.get(info), values

    assert len(rows) == 99  # 54 on the mesh, 30 on the curves, 15 on the point cloud
    failures = fixture.compare(rows, lookup)
    assert not failures, "\n".join(failures)
    for obj, names in absent.items():
        present = set(objects.get(obj, {}).get("attribs", {}))
        assert not present & set(names), (obj, sorted(present & set(names)))
    for obj, expected in groups.items():
        assert objects[obj]["prim_groups"] == expected["prims"]
        assert objects[obj]["edge_groups"] == {k: [sorted(p) for p in v] for k, v in expected["edges"].items()}


def test_nothing_is_dropped_silently(converted):
    """Every Blender attribute either arrives (test above) or is named, with its reason, in one
    import-log line per object."""
    fixture = _fixture()
    *_, not_imported = fixture.expectations(converted["expected"])
    parsed = dict(fixture.parse_line(line) for line in converted["lines"])
    assert parsed == not_imported
    assert len(converted["lines"]) == len(not_imported)  # one line per object


def test_default_radius_is_blenders(converted):
    """Curves and point clouds without a radius get Blender's own radius (export_hair_curves:
    0.005, export_pointcloud: 0.01), so Converted renders as thick as Link and Blender."""
    curves = converted["objects"]["AttrCurves"]["attribs"]["width"]
    points = converted["objects"]["AttrPoints"]["attribs"]["pscale"]
    assert [value[0] for value in curves[4]] == pytest.approx([0.01] * 7)
    assert [value[0] for value in points[4]] == pytest.approx([0.01] * 5)
    notes = converted["report"]["notes"]
    assert any(note.startswith("AttrCurves: no radius attribute") for note in notes)
    assert any(note.startswith("AttrPoints: no radius attribute") for note in notes)
