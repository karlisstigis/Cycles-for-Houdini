# SPDX-License-Identifier: Apache-2.0
"""Opt-in incremental native recipes; ordinary nightly pytest never collects these."""

import pytest

from support.catalogue import case_id, catalogue_cases, headless_recipes, required_cases
from support.catalogue_pytest import catalogue_queue
from support.recipe_contracts import AUDITED_RECIPES


def pytest_generate_tests(metafunc):
    if "catalogue_case" not in metafunc.fixturenames:
        return
    if not metafunc.config.getoption("--catalogue"):
        metafunc.parametrize("catalogue_case", [])
        return
    recipes = {row["test"]: row for row in headless_recipes()}
    cases = []
    for case in catalogue_cases():
        if case["test"] in AUDITED_RECIPES:
            continue
        marks = [pytest.mark.hython, pytest.mark.slow]
        if case["device"] in ("CPU", "OPTIX"):
            marks.append(pytest.mark.cpu if case["device"] == "CPU" else pytest.mark.optix)
        if "blender" in recipes[case["test"]]["prerequisites"]:
            marks.append(pytest.mark.blender)
        cases.append(pytest.param(case, id=case_id(case), marks=marks))
    metafunc.parametrize("catalogue_case", cases)


def test_catalogue_headless_recipe(catalogue_case, catalogue_queue):
    for required in required_cases([catalogue_case]):
        entry = catalogue_queue[(required["test"], required["device"])]
        assert entry["result"] == "passed" and entry["exit_code"] == 0, entry
