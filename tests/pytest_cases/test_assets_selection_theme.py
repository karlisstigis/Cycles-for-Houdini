# SPDX-License-Identifier: Apache-2.0
"""Selected and hovered items of the asset browser follow the Houdini theme, not its orange accent.

Houdini 22.0.466 (hutil.qt.pluto.parts.drawTabShape) draws a selected entry as `checkedSurface`
blended half-way into `surfaceHighest` with a `lighter(110)` border, and a hovered one as a faint
veil; the accent `highlight` (#fdba00 in the default theme) is for menus. The asset browser
derives its own tokens from the shared theme's `menu` (surfaceHighest) and `active`
(checkedSurface) and never uses `selection` (the render viewer's token).
"""

import os

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtGui  # noqa: E402

from cycles_houdini.assets import widgets  # noqa: E402
from cycles_houdini.viewer.viewer_theme import contrast_ratio  # noqa: E402

# Houdini 22.0.466 default theme (config/Themes/PlutoThemeDefaults.json) in the shared theme roles.
DEFAULT = {
    "background": "#2d2d2d",
    "toolbar": "#2d2d2d",
    "canvas": "#282828",
    "field": "#434343",
    "numeric": "#434343",
    "dropdown_hi": "#474e62",
    "dropdown_lo": "#474e62",
    "border": "#202020",
    "divider": "#202020",
    "text": "#dddddd",
    "muted": "#656565",
    "disabled": "#4e4e4e",
    "button": "#474e62",
    "hover": "#555e76",
    "hover_border": "#777f95",
    "active": "#47578b",
    "active_border": "#6f7997",
    "active_text": "#e3ebff",
    "menu": "#383838",
    "selection": "#fdba00",
    "selection_text": "#271900",
    "progress": "#7082b9",
    "warning": "#ff0000",
    "thumb": "#777f95",
    "pressed": "#313f71",
    "active_hover": "#4e6099",
}


def theme(values=DEFAULT):
    return {key: QtGui.QColor(value) for key, value in values.items()}


def test_item_tokens_come_from_surface_and_checked_surface_not_the_accent():
    assert widgets.ITEM_SELECTED_ROLES == ("menu", "active")
    items = widgets.item_colors(theme())
    assert items["item_selected"].name() == "#404862"  # #383838 and #47578b, half-way
    assert items["item_selected_border"] == items["item_selected"].lighter(110)
    orange = QtGui.QColor("#fdba00")
    assert all(color.name() != orange.name() for color in items.values())
    # Text keeps the theme's own colour and stays readable on the selected surface.
    assert items["item_selected_text"].name() == "#dddddd"
    assert contrast_ratio(items["item_selected_text"], items["item_selected"]) >= 4.5
    # Hover is quieter than the selection: closer to the background it sits on.
    background = QtGui.QColor(DEFAULT["background"])
    assert contrast_ratio(items["item_hover"], background) < contrast_ratio(items["item_selected"], background)
    assert items["item_hover"] != background
    grid = widgets.item_colors(theme(), "canvas")
    assert grid["item_hover"] != items["item_hover"]  # the grid veils its own canvas


def test_style_sheet_uses_the_item_tokens_and_never_the_viewer_selection():
    sheet = widgets.asset_style(theme())
    items = widgets.item_colors(theme())
    assert "#fdba00" not in sheet and "#271900" not in sheet
    assert f"::item:selected {{ background: {items['item_selected'].name()}" in sheet
    assert f"::item:hover {{ background: {items['item_hover'].name()}" in sheet
    assert f"selection-background-color: {items['item_selected'].name()}" in sheet


def test_a_light_theme_keeps_readable_selected_text():
    light = dict(DEFAULT, background="#e8e8e8", canvas="#f0f0f0", menu="#ffffff", active="#a8b8ec", text="#202020")
    items = widgets.item_colors(theme(light))
    assert contrast_ratio(items["item_selected_text"], items["item_selected"]) >= 4.5
    assert items["item_hover"].lightness() < QtGui.QColor("#e8e8e8").lightness()  # a veil of dark text


def test_grid_delegate_paints_selection_with_the_item_tokens(monkeypatch):
    """The thumbnail delegate reads item_colors, not theme["selection"]."""
    import inspect

    source = inspect.getsource(widgets)
    paint = source[source.index("    def paint(self, painter, option, index):") :]
    paint = paint[: paint.index("    def _paint_badge")]
    assert 'item_colors(theme, "canvas")' in paint
    assert 'theme["selection"]' not in paint and 'theme["selection_text"]' not in paint
