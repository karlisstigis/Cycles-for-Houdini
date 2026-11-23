# SPDX-License-Identifier: Apache-2.0
"""Link mode reads a hair or point-cloud "radius" as Blender v5.3.0's Cycles translator does
(task L1): through lookup<float>("radius", Point) (intern/cycles/blender/curves.cpp
export_hair_curves, pointcloud.cpp export_pointcloud), so a radius stored per curve, or as an
integer, boolean, vector or colour, is used too, converted as Blender's implicit conversions
do (vectors average their components, colours take the luminance); a quaternion, matrix or
string radius is not, and the default (0.005 for hair, 0.01 for points) applies. Link and Convert & Append (_converter_radius) agree.

The bpy case builds the datablocks with Blender 5.3 (the PyPI bpy module) and calls the
exporter's record functions directly. The render comparison is the "Radii" asset of
tests/houdini_blend_sync_parity_smoke.py.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, sys
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
import numpy as np
from cycles_blender import blend_export

bpy.ops.wm.read_factory_settings(use_empty=True)


def radius(data, kind, domain, values):
    if "radius" in data.attributes:
        data.attributes.remove(data.attributes["radius"])
    if kind is not None:
        attribute = data.attributes.new("radius", kind, domain)
        prop = {"FLOAT_VECTOR": "vector", "FLOAT2": "vector", "FLOAT4": "vector", "FLOAT_COLOR": "color"}
        attribute.data.foreach_set(prop.get(kind, "value"), values)
    return data


def hair(kind, domain, values):
    data = bpy.data.hair_curves.new("Hair")
    data.add_curves([2, 3])
    data.attributes["position"].data.foreach_set("vector", np.arange(15, dtype=np.float32))
    return radius(data, kind, domain, values)


def cloud(kind, values):
    data = bpy.data.pointclouds.new("Points")
    data.resize(3)
    data.attributes["position"].data.foreach_set("vector", np.arange(9, dtype=np.float32))
    return radius(data, kind, "POINT", values)


cases = dict(
    hair_point_float=hair("FLOAT", "POINT", [0.1, 0.2, 0.3, 0.4, 0.5]),
    hair_curve_float=hair("FLOAT", "CURVE", [0.25, 0.5]),
    hair_point_int=hair("INT", "POINT", [1, 2, 0, 1, 3]),
    hair_curve_int=hair("INT", "CURVE", [2, 1]),
    hair_point_bool=hair("BOOLEAN", "POINT", [True, False, True, True, False]),
    hair_vector=hair("FLOAT_VECTOR", "POINT", [0.2, 0.4, 1.2] * 5),
    hair_float2=hair("FLOAT2", "POINT", [0.4, 0.8] * 5),
    hair_float4=hair("FLOAT4", "CURVE", [0.2, 0.4, 0.6, 1.2, 0.0, 0.0, 0.0, 4.0]),
    hair_int2=hair("INT32_2D", "POINT", [0, 1] * 5),
    hair_color=hair("FLOAT_COLOR", "POINT", [1.0, 0.5, 0.2, 0.3] * 5),
    hair_quaternion=hair("QUATERNION", "POINT", [0.5] * 20),
    hair_none=hair(None, None, None),
    points_float=cloud("FLOAT", [0.1, 0.2, 0.3]),
    points_int=cloud("INT", [1, 0, 2]),
    points_int8=cloud("INT8", [3, 1, 2]),
    points_none=cloud(None, None),
)
result = {}
for name, data in cases.items():
    arrays = {}
    if name.startswith("hair"):
        record = blend_export._hair_curves_record(data, "k", arrays, "THICK")
        converted = blend_export._curves_arrays(data, "c", arrays)
    else:
        record = blend_export._point_cloud_record(data, "k", arrays, set())
        converted = None
    result[name] = dict(
        link=np.asarray(arrays[record["arrays"]["radius"]]).tolist(),
        max_radius=record["max_radius"],
        converted=np.asarray(arrays[converted["arrays"]["radius"]]).tolist() if converted else None,
        attributes=[field["name"] for field in record["attributes"]],
    )
print("RESULT:" + json.dumps(result))
"""

EXPECTED = dict(
    hair_point_float=[0.1, 0.2, 0.3, 0.4, 0.5],
    hair_curve_float=[0.25, 0.25, 0.5, 0.5, 0.5],
    hair_point_int=[1, 2, 0, 1, 3],
    hair_curve_int=[2, 2, 1, 1, 1],
    hair_point_bool=[1, 0, 1, 1, 0],
    hair_vector=[0.6] * 5,
    hair_float2=[0.6] * 5,
    hair_float4=[0.6, 0.6, 1.0, 1.0, 1.0],
    hair_int2=[0.5] * 5,
    hair_color=[0.2126 + 0.7152 * 0.5 + 0.0722 * 0.2] * 5,  # Rec.709 luma, Blender's default config
    hair_quaternion=[0.005] * 5,
    hair_none=[0.005] * 5,
    points_float=[0.1, 0.2, 0.3],
    points_int=[1, 0, 2],
    points_int8=[3, 1, 2],
    points_none=[0.01] * 3,
)


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_radius")
    script = folder / "make_and_export.py"
    script.write_text(SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT / "houdini/python3.13libs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


@pytest.mark.blender
@pytest.mark.cpu
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_link_radius_is_blenders_float_lookup(exported, name):
    case = exported[name]
    assert case["link"] == pytest.approx(EXPECTED[name])
    assert case["max_radius"] == pytest.approx(max(EXPECTED[name]))
    assert "radius" not in case["attributes"]  # Cycles' standard radius, not a named attribute
    if case["converted"] is not None:
        assert case["converted"] == case["link"]  # Link and Convert & Append agree
