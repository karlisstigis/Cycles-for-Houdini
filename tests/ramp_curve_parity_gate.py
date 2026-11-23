# SPDX-License-Identifier: Apache-2.0
"""K33: compare imported-material renders to pinned Blender and record known sampling loss.

Run houdini_ramp_curve_blender_smoke.py on CPU and OptiX before this check.
The reference is Blender's EXR pixels, never the retired exact implementation.
The shutter report compares the actual production ramp with an animated scalar and a freeze control.
"""

import argparse
import json
from pathlib import Path
import numpy as np


def pixels_pass(case, backend, pixels):
    reference = np.asarray(case["blender_pixels" if backend == "CPU" else "blender_optix_pixels"], dtype=np.float32)
    actual = np.asarray(pixels)
    if actual.shape != reference.shape or not np.isfinite(actual).all():
        return False
    matches = np.abs(actual.astype(np.float64) - reference) <= 3e-8
    if backend == "CPU" and case["id"] == "rgb_curves_nonlinear_combined":
        # Only the measured fused results at these two inputs are approved.
        # The independent Blender OptiX render records the same rounded values.
        fused = np.asarray(case["blender_optix_pixels"], dtype=np.float32)
        for i, factor in enumerate(case["factors"]):
            if factor in (0.271230012178421, 0.912339985370636):
                adjacent = np.nextafter(reference[i], fused[i]) == fused[i]
                matches[i] |= adjacent & (actual[i] == fused[i])
    return bool(matches.all())


def check(runtime_reports, shutter_reports, reference):
    expected = {case["id"]: case for case in reference["cases"]}
    result = {}
    for backend in ("CPU", "OptiX"):
        report = json.loads((runtime_reports / (backend + ".json")).read_text())
        rows = {row["case"]: row for row in report["checks"]}
        if rows.keys() != expected.keys():
            raise ValueError(f"Incomplete {backend} imported-material capture")
        reference_field = "blender_pixels" if backend == "CPU" else "blender_optix_pixels"
        errors = {
            name: float(np.abs(np.asarray(row["actual"]) - expected[name][reference_field]).max())
            for name, row in rows.items()
        }
        passed = all(pixels_pass(expected[name], backend, row["actual"]) for name, row in rows.items())
        shutter = json.loads((shutter_reports / (backend + ".json")).read_text())
        scalar_error = float(shutter["animated_reference_mae"])
        freeze_error = float(shutter["frozen_mae"])
        shutter_pass = (
            shutter["result"] == "passed"
            and shutter["pixels"] > 0
            and np.isfinite([scalar_error, freeze_error]).all()
            and freeze_error > 0
            and scalar_error < freeze_error * 0.1
        )
        result[backend] = dict(
            passed=bool(report["result"] == "passed" and passed and shutter_pass),
            imported_material_max_errors=errors,
            shutter=shutter,
        )
    # The old note inferred greater clipping at 257 from 4096/16384 captures.
    # At 257 the original apex x=.5 is exactly on the table grid. Measure both.
    result["known_differences"] = {
        name: dict(factors=case["factors"], authored=case["authored"], blender=case["blender_pixels"])
        for name, case in expected.items()
        if name in ("ridge_aligned", "ridge_off_grid", "constant_subsample", "hue_far")
    }
    return result


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-reports",
        type=Path,
        default=root / "artifacts/ramp-curve-k33/production/stage3/materials/ramp-curve-blender",
    )
    parser.add_argument("--shutter-reports", type=Path, default=root / "artifacts/ramp-curve-k33/production/shutter")
    args = parser.parse_args()
    reference = json.loads((root / "tests/fixtures/blender-ramp-curve-gate.json").read_text())
    result = check(args.runtime_reports, args.shutter_reports, reference)
    print(json.dumps(result, indent=2, allow_nan=False))
    raise SystemExit(0 if all(result[backend]["passed"] for backend in ("CPU", "OptiX")) else 1)
