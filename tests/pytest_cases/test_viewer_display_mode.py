# SPDX-License-Identifier: Apache-2.0
"""Viewer display switch and the GPU canvas's texture update bookkeeping.

Drawing itself is measured in Houdini by tests/houdini_viewer_display_modes_smoke.py.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


@pytest.fixture(scope="module")
def display():
    package = ModuleType("viewer_display_pkg")
    package.__path__ = []
    panel = ModuleType("viewer_display_pkg.panel")
    panel.CanvasBehaviour = type("CanvasBehaviour", (), {})
    saved = {name: sys.modules.get(name) for name in (package.__name__, panel.__name__)}
    sys.modules[package.__name__] = package
    sys.modules[panel.__name__] = panel
    try:
        spec = importlib.util.spec_from_file_location("viewer_display_pkg.viewer_display", VIEWER / "viewer_display.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_mode_default_and_choices(display):
    assert display.mode({}) == ("gpu", "")
    assert display.mode({display.ENVIRONMENT: " Classic "}) == ("classic", "")
    assert display.mode({display.ENVIRONMENT: "native"}) == ("native", "")
    assert display.mode({display.ENVIRONMENT: "gpu"}) == ("gpu", "")
    chosen, note = display.mode({display.ENVIRONMENT: "vulkan"})
    assert chosen == display.DEFAULT and "vulkan" in note


def test_damage_chains_consecutive_merges(display):
    first = display.damage_union(None, 1, 2, (10, 10, 5, 5))
    assert first == (1, 2, (10, 10, 5, 5))
    chained = display.damage_union(first, 2, 3, (0, 12, 4, 10))
    assert chained == (1, 3, (0, 10, 15, 12))
    # A merge that does not continue the chain starts a new one.
    assert display.damage_union(chained, 7, 8, (0, 0, 1, 1)) == (7, 8, (0, 0, 1, 1))
    # A freshly allocated image has no previous key: full upload.
    assert display.damage_union(chained, None, 9, (0, 0, 1, 1)) is None


def test_upload_plan(display):
    plan = display.upload_plan
    assert plan(None, None, 5, (4, 4), None) == "full"
    assert plan(5, (4, 4), 5, (4, 4), None) is None
    assert plan(5, (4, 4), 6, (4, 4), None) == "full"
    assert plan(5, (4, 4), 6, (8, 4), (5, 6, (0, 0, 1, 1))) == "full"
    assert plan(5, (4, 4), 6, (4, 4), (5, 6, (1, 1, 2, 2))) == (1, 1, 2, 2)
    assert plan(5, (4, 4), 6, (4, 4), (5, 6, (-2, 3, 10, 10))) == (0, 3, 4, 1)
    # Damage from another base (texture missed a merge) needs everything.
    assert plan(4, (4, 4), 6, (4, 4), (5, 6, (1, 1, 2, 2))) == "full"
