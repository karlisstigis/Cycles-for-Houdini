# SPDX-License-Identifier: Apache-2.0
"""The viewer chrome follows its monitor: lengths are the font pixel size / 12.

Measured in Houdini by tests/houdini_viewer_chrome_scale_gui_smoke.py (4K at
144 dpi primary, HD at 96 dpi, Houdini font 18 px: 36 px toolbar wrappers and 30 px buttons on
the 4K, 24 px and 20 px on the HD, the same millimetres on both).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


def load(name):
    package = sys.modules.setdefault("chrome_scale_pkg", ModuleType("chrome_scale_pkg"))
    package.__path__ = []
    spec = importlib.util.spec_from_file_location("chrome_scale_pkg." + name, VIEWER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def qt():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_monitor_rule_keeps_physical_size():
    ui_scale = load("ui_scale")
    reference = 144.0  # Primary 4K monitor at 150 % (Qt scaling off: DPR 1).
    for dpi, font, wrapper, button in ((144.0, 18, 36, 30), (96.0, 12, 24, 20)):
        factor = ui_scale.logical_scale(dpi, reference)
        pixels = max(1, round(18 * factor))
        assert pixels == font
        assert max(1, round(24 * pixels / 12)) == wrapper
        assert max(1, round(20 * pixels / 12)) == button
        assert wrapper / dpi * 25.4 == pytest.approx(6.35)
        assert button / dpi * 25.4 == pytest.approx(20 / 96 * 25.4)


@pytest.mark.parametrize("scale", [2 / 3, 1.0, 1.5])
def test_toolbar_row_is_never_taller_than_its_scaled_controls(qt, scale):
    from PySide6 import QtWidgets

    toolbar = load("viewer_toolbar")
    chrome = load("viewer_chrome")
    panel = QtWidgets.QWidget()
    panel.exposure = SimpleNamespace(input=QtWidgets.QWidget(panel))
    panel.gamma = SimpleNamespace(input=QtWidgets.QWidget(panel))
    row = toolbar.ToolbarRow()
    row.setParent(panel)
    button = QtWidgets.QToolButton()
    row.group(button)
    panel.all_toolbar_rows = [row]
    chrome.apply_metrics(panel, scale)
    margins = round(3 * scale) * 2
    row.row.setContentsMargins(8, round(3 * scale), 8, round(3 * scale))
    expected = max(1, round(24 * scale))
    assert button.height() == expected
    assert row.min_content_height == expected
    # Before the fix the row kept an unscaled 24 px floor: 28 px rows around
    # 16 px buttons at a 2/3 scale (panel_navigation_checks.check_layout).
    assert row.sizeHint().height() == expected + margins
