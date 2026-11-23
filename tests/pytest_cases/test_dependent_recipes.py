# SPDX-License-Identifier: Apache-2.0
"""Audited domain recipes share one producer queue and keep each native process fresh."""

import json

import pytest

from support.catalogue import case_id, catalogue_cases
from support.catalogue_pytest import catalogue_queue
from support.recipe_contracts import AUDITED_RECIPES, audit_recipe


def pytest_generate_tests(metafunc):
    if "recipe_case" not in metafunc.fixturenames:
        return
    if not metafunc.config.getoption("--catalogue"):
        metafunc.parametrize("recipe_case", [])
        return
    recipe = metafunc.function.__name__.removeprefix("test_") + ".py"
    assert recipe in AUDITED_RECIPES
    cases = []
    for case in catalogue_cases():
        if case["test"] == recipe:
            marks = [pytest.mark.hython, pytest.mark.slow]
            if case["device"] in ("CPU", "OPTIX"):
                marks.append(pytest.mark.cpu if case["device"] == "CPU" else pytest.mark.optix)
            cases.append(pytest.param(case, id=case_id(case), marks=marks))
    metafunc.parametrize("recipe_case", cases)


@pytest.fixture
def recipe_report(recipe_case, catalogue_queue):
    row = catalogue_queue[(recipe_case["test"], recipe_case["device"])]
    assert row["result"] == "passed" and row["exit_code"] == 0, row
    return json.loads(row["evidence_path"].read_text(encoding="utf-8-sig"))


def test_houdini_panel_motion_timing_smoke(recipe_case, recipe_report):
    """Thirteen independent shutter/FPS coverages retain the same renderer."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_houdini_transparency_batch_smoke(recipe_case, recipe_report):
    """Surface association and zero-alpha emission survive animated SOHO output."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_houdini_queued_worker_smoke(recipe_case, recipe_report):
    """Courtyard ownership/counts, independent RGBA, coalescing and worker recovery."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_houdini_light_parenting_smoke(recipe_case, recipe_report):
    """Fresh named lights and full capture match independently parented stock lights."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_houdini_texture_cache_smoke(recipe_case, recipe_report):
    """All cache routes retain analytic revision colors and derivative isolation."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_houdini_texture_normal_smoke(recipe_case, recipe_report):
    """OpenGL, DirectX, world normals and both bump slopes match analytic vectors."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_houdini_texture_auto_batch_smoke(recipe_case, recipe_report):
    """Both cache paths switch Auto color-space rules and repair missing input."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_core_strand_material_smoke(recipe_case, recipe_report):
    """Native point/curve images agree with independent harness palette images."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_core_volume_material_smoke(recipe_case, recipe_report):
    """Absorption, emission and blackbody match separate harness scenes."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_core_displacement_smoke(recipe_case, recipe_report):
    """Positioned meshes/seams match independent RGBA and interior data planes."""
    audit_recipe(recipe_case["test"], recipe_report)


def test_core_subdivision_smoke(recipe_case, recipe_report):
    """Catmull-Clark limit images match a separately refined polygon reference."""
    audit_recipe(recipe_case["test"], recipe_report)
