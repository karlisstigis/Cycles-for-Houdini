# SPDX-License-Identifier: Apache-2.0
"""Version 1 shader-curve sources (authored Blender Bezier points) against Blender's own tables.

Builds a version 1 source (the record an importer writes for a CurveMapping
without a baked table) for every case of tests/fixtures/blender-shader-curves.json,
runs it through the compiled cycles_houdini._native.shader_curve_table, and
compares the 257-row table with the one Blender 5.2 evaluated. Tolerance from
docs/dev/provenance/curve-mapping.md section 8.1: |ours - Blender| <= 2e-5 *
max(1, |Blender|). A wrong handle, sampling or extension rule moves values by
1e-3 or more and fails here with the case, channel and row named.
"""

from support.paths import artifacts_root
import json
import os
import struct
from pathlib import Path
import numpy as np
import cycles_houdini as ch

MAGIC = 0x43435231
KINDS = ("float", "rgb", "vector")
HANDLES = {"AUTO": 0, "AUTO_CLAMPED": 1, "VECTOR": 2}
TOLERANCE = 2e-5
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
fixture = json.loads((root / "tests/fixtures/blender-shader-curves.json").read_text(encoding="utf-8"))
destination = artifacts_root() / "stage3/materials/curve-mapping"
destination.mkdir(parents=True, exist_ok=True)


def word(value):
    return struct.unpack("<i", struct.pack("<f", float(value)))[0]


def version1_source(case, channels=None):
    channels = case["channels"] if channels is None else channels
    source = [MAGIC, 1, KINDS.index(case["kind"]), int(case["extension"] == "EXTRAPOLATED")]
    source += [word(case["domain"][0]), word(case["domain"][1]), 0, len(channels)]
    for points in channels:
        source.append(len(points))
        for x, y, handle in points:
            source += [word(x), word(y), HANDLES[handle]]
    return source


def table(case, channels=None):
    return json.loads(ch._native.shader_curve_table(version1_source(case, channels), case["kind"]))


assert len(fixture["cases"]) == 27, len(fixture["cases"])
results, failures = [], []
for case in fixture["cases"]:
    ours = table(case)
    expected = np.asarray(case["table"], np.float32)
    actual = np.asarray(ours["table"], np.float32)
    assert actual.shape == expected.shape == (257, 3), (case["id"], actual.shape, expected.shape)
    assert ours["minimum"] == np.float32(case["minimum"]) and ours["maximum"] == np.float32(case["maximum"]), (
        case["id"],
        ours["minimum"],
        ours["maximum"],
        case["minimum"],
        case["maximum"],
    )
    assert ours["extrapolate"] == (case["extension"] == "EXTRAPOLATED"), case["id"]
    scaled = np.abs(actual.astype(np.float64) - expected) / np.maximum(1.0, np.abs(expected.astype(np.float64)))
    row, column = np.unravel_index(int(scaled.argmax()), scaled.shape)
    worst = dict(
        case=case["id"],
        worst_scaled_difference=float(scaled[row, column]),
        row=int(row),
        column=int(column),
        ours=float(actual[row, column]),
        blender=float(expected[row, column]),
    )
    results.append(worst)
    if worst["worst_scaled_difference"] > TOLERANCE:
        failures.append(worst)

# This project's own rules for bad artist data (section 1.1): a local source
# error, which the binding reports as RuntimeError with the source's message.
case = fixture["cases"][0]
duplicate = [[list(p) for p in case["channels"][0]]]
duplicate[0][1][0] = duplicate[0][2][0]
try:
    table(case, duplicate)
except RuntimeError as error:
    assert "distinct X" in str(error), str(error)
else:
    raise AssertionError("Duplicate X positions were accepted")

report = dict(
    result="failed" if failures else "passed",
    scope="Version 1 shader-curve sources against Blender 5.2 CurveMapping.evaluate tables",
    fixture="tests/fixtures/blender-shader-curves.json",
    tolerance=dict(atol=TOLERANCE, rtol=TOLERANCE),
    cases=len(results),
    values=len(results) * 257 * 3,
    worst=max(results, key=lambda item: item["worst_scaled_difference"]),
    failures=failures,
    per_case=results,
)
(destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps({key: report[key] for key in ("result", "cases", "values", "worst", "failures")}))
assert not failures, json.dumps(failures, indent=2)
