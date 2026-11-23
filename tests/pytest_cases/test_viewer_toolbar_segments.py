# SPDX-License-Identifier: Apache-2.0
"""Joined toolbar buttons are segmented controls: rounded outer corners only.

A compact transport cluster (Render, Denoise/Show Noisy, dicing, checker,
resolution) rounds the first button's left corners and the last button's
right corners through the cycles_transport_start / cycles_transport_end
properties. Pick tools commit a14e963 dropped Show Noisy's end property and
the Denoise pair's right edge became square. The style sheet rule is checked
here with real widgets at chrome scales 1, 1.5 and 2 (the corner radius is
scaled with the chrome); viewer_layout.apply's properties are checked in
Houdini by tests/houdini_viewer_toolbar_layout_gui_smoke.py.
"""

import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


def load(name):
    package = sys.modules.setdefault("toolbar_segments_pkg", ModuleType("toolbar_segments_pkg"))
    package.__path__ = [str(VIEWER)]
    import importlib

    return importlib.import_module("toolbar_segments_pkg." + name)


@pytest.fixture(scope="module")
def qt():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pair(scale, end):
    from PySide6 import QtCore, QtGui, QtWidgets

    chrome = load("viewer_chrome")
    layout = load("viewer_layout")
    ui_scale = load("ui_scale")
    root = QtWidgets.QWidget()
    root.setProperty("cycles_render_panel", True)
    root.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    font = QtGui.QFont(root.font())
    font.setPixelSize(max(1, round(12 * scale)))
    root.setFont(font)
    theme = {key: QtGui.QColor(value) for key, value in load("viewer_theme").read_theme().items()}
    # Distinct wrapper and button fills so a square corner is visible in one pixel.
    theme.update(toolbar=QtGui.QColor("#202020"), field=QtGui.QColor("#a0a0a0"), border=QtGui.QColor("#202020"))
    root.setStyleSheet(ui_scale.scaled_style(layout.redesign_style(theme), scale))
    row = QtWidgets.QHBoxLayout(root)
    left, right = chrome.ViewerToolButton(), chrome.ViewerToolButton()
    left.setProperty("cycles_transport_start", True)
    if end:
        right.setProperty("cycles_transport_end", True)
    frame = chrome.cluster(left, right, kind="transport", compact=True)
    row.addWidget(frame)
    chrome.apply_dimensions(root, scale)
    root.show()
    QtWidgets.QApplication.processEvents()
    return root, frame, left, right


def corner(frame, button, x_right):
    from PySide6 import QtCore

    image = frame.grab().toImage()
    ratio = image.devicePixelRatio()
    rect = QtCore.QRect(button.mapTo(frame, QtCore.QPoint()), button.size())
    x = rect.right() if x_right else rect.left()
    top = image.pixelColor(int(x * ratio), int(rect.top() * ratio))
    fill = image.pixelColor(int(x * ratio), int(rect.center().y() * ratio))
    return top, fill


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_pair_rounds_both_outer_corners(qt, scale):
    root, frame, left, right = pair(scale, end=True)
    try:
        assert right.height() == max(1, round(20 * scale))
        for button, x_right in ((left, False), (right, True)):
            top, fill = corner(frame, button, x_right)
            assert fill.lightness() > 120, fill.name()
            # The outer corner pixel is outside the rounded rectangle: wrapper colour.
            assert top.lightness() < fill.lightness() - 40, (scale, x_right, top.name(), fill.name())
        # Inner corners, where the two buttons join, stay square.
        top, fill = corner(frame, left, True)
        assert top == fill, (top.name(), fill.name())
    finally:
        root.deleteLater()


def test_missing_end_property_leaves_square_corner(qt):
    """The regression this file guards: without the end property the right edge is square."""
    root, frame, left, right = pair(1.0, end=False)
    try:
        top, fill = corner(frame, right, True)
        assert top == fill, (top.name(), fill.name())
    finally:
        root.deleteLater()
