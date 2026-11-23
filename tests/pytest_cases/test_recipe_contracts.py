# SPDX-License-Identifier: Apache-2.0
"""The domain checks reject plausible passing reports with corrupted measurements."""

import json
import math

import pytest

from support.paths import ROOT
from support.recipe_contracts import AUDITED_RECIPES, DEPENDENT_RECIPES, audit_recipe

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def historical_controls():
    reports = json.loads((ROOT / "tests/fixtures/dependent-recipe-evidence.json").read_text())["reports"]
    # Preserve the frozen execution evidence. Current corner-normal and strand
    # contracts add synthetic controls here, not new execution claims.
    hierarchy = (
        "nested rotated parent",
        "nonuniform parent",
        "mirrored parent",
        "reparent",
        "disconnect parent",
        "restore parent",
    )
    shapes = ("rectangle", "ellipse", "square", "disk")
    labels = [(label, 1) for label in hierarchy]
    labels += [("Look At under an animated mirrored nonuniform parent", frame) for frame in (1, 5, 10)]
    labels += [("native " + shape + " area size and aim", 10) for shape in shapes]
    cases = []
    for case, frame in labels:
        pose = [1, 0, 0, frame, 0, 1, 0, 0, 0, 0, 1, 0]
        cases.append(dict(case=case, frame=frame, pose=pose, stock_pose=pose.copy(), max_matrix_error=0))
    reports["houdini_light_parenting_smoke.py"] = dict(result="passed", cases=cases)
    for device in reports["core_strand_material_smoke.py"]["reports"]:
        for event in device["events"]:
            # The current harness replaces two obsolete volume-domain rejects
            # with diagnostic render/repair checks for each geometry kind.
            event["checks"] = 30 if "kind" not in event else 15
    for device in reports["houdini_texture_normal_smoke.py"]["devices"]:
        for normal in ((0, 0, 1), (0.3, 0.4, math.sqrt(0.75))):
            for basis in ("original", "displaced"):
                device["checks"].append(
                    dict(
                        name="Static corner normals survive a neutral tangent normal map",
                        basis=basis,
                        authored_normal=list(normal),
                        measured=[value * 0.5 + 0.5 for value in normal],
                    )
                )
        normal = (0.25, 0.1, 1)
        length = math.sqrt(sum(value * value for value in normal))
        reference = [value / length * 0.5 + 0.5 for value in normal]
        for basis in ("original", "displaced"):
            device["checks"].append(
                dict(
                    name="Moving corner normals preserve ray-time shading with a neutral map",
                    basis=basis,
                    measured=list(reference),
                    reference=list(reference),
                )
            )
    return reports


@pytest.mark.parametrize("recipe", list(AUDITED_RECIPES))
def test_domain_contract_accepts_audited_numerical_controls(recipe):
    assert audit_recipe(recipe, historical_controls()[recipe])["domain"] == AUDITED_RECIPES[recipe]


@pytest.mark.parametrize("recipe", list(AUDITED_RECIPES))
def test_domain_contract_rejects_corrupted_measurements_despite_generic_pass(recipe):
    report = historical_controls()[recipe]
    if recipe == "houdini_panel_motion_timing_smoke.py":
        report["checks"][3]["measured_red"] = 0.32
    elif recipe == "houdini_light_parenting_smoke.py":
        report["cases"][0]["pose"][3] = 0.01
    elif recipe == "houdini_texture_cache_smoke.py":
        report["cases"][0]["updated"] = [0.2, 0.4, 0.8]
    elif recipe == "houdini_texture_normal_smoke.py":
        report["devices"][0]["checks"][1]["measured"] = report["devices"][0]["checks"][0]["measured"]
    elif recipe == "houdini_texture_auto_batch_smoke.py":
        report["cases"][0]["frames"][1]["input_space"] = "Linear Rec.709 (sRGB)"
    elif recipe == "houdini_transparency_batch_smoke.py":
        report["cases"][0]["frames"][0]["background_rgba"][-1] = 1
    elif recipe == "houdini_queued_worker_smoke.py":
        report["images"]["worker"]["baseline_rmse"] = 0.5
    elif recipe == "core_subdivision_smoke.py":
        report["reports"][0]["interior_data"]["normal.Z"]["maximum_absolute_difference"] = 0.2
    else:
        report["reports"][0]["images"][0]["maximum_absolute_rgba_difference"] = 0.5
    assert report["result"] == "passed"
    with pytest.raises(ValueError):
        audit_recipe(recipe, report)


def test_recipe_inventory_matches_every_declared_dependency_and_preserves_process_ownership():
    from support.catalogue import headless_recipes

    assert set(DEPENDENT_RECIPES) == {row["test"] for row in headless_recipes() if row["depends_on"]}
    # The domain audits add no imports/evaluation of the native fixture scripts.
    import sys

    assert not ({name.removesuffix(".py") for name in AUDITED_RECIPES} & set(sys.modules))


@pytest.mark.parametrize("missing", range(11))
def test_normal_contract_rejects_every_missing_category_or_basis(missing):
    report = historical_controls()["houdini_texture_normal_smoke.py"]
    report["devices"][0]["checks"].pop(missing)
    with pytest.raises(ValueError, match="incomplete"):
        audit_recipe("houdini_texture_normal_smoke.py", report)


@pytest.mark.parametrize("checks", [26, 28, 29])
def test_strand_contract_rejects_missing_domain_recovery_and_repair_checks(checks):
    report = historical_controls()["core_strand_material_smoke.py"]
    report["reports"][0]["events"][-1]["checks"] = checks
    with pytest.raises(ValueError, match="Independent harness checks changed"):
        audit_recipe("core_strand_material_smoke.py", report)


@pytest.mark.parametrize(
    "corruption",
    [
        "static-basis",
        "static-authored",
        "static-measured",
        "moving-basis",
        "moving-measured",
        "moving-reference",
        "category",
    ],
)
def test_normal_contract_rejects_corrupted_corner_normal_evidence(corruption):
    report = historical_controls()["houdini_texture_normal_smoke.py"]
    checks = report["devices"][1]["checks"]
    if corruption == "static-basis":
        checks[6]["basis"] = checks[5]["basis"]
    elif corruption == "static-authored":
        checks[7]["authored_normal"] = checks[5]["authored_normal"]
    elif corruption == "static-measured":
        checks[7]["measured"] = checks[5]["measured"]
    elif corruption == "moving-basis":
        checks[10]["basis"] = checks[9]["basis"]
    elif corruption == "moving-measured":
        checks[10]["measured"] = [0.5, 0.5, 1]
    elif corruption == "moving-reference":
        checks[10]["reference"][0] = float("nan")
    else:
        checks[9]["name"] = checks[5]["name"]
    with pytest.raises(ValueError):
        audit_recipe("houdini_texture_normal_smoke.py", report)
