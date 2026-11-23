# SPDX-License-Identifier: Apache-2.0
"""Theme-tinted SVG assets, letterboxed without changing their proportions."""

from pathlib import Path
import re

from PySide6 import QtCore, QtGui, QtSvg

ASSET_ROOT = Path(__file__).resolve().parents[3] / "config/Icons/cycles_viewer/plain"
_ASSETS = {
    "IMAGE_final_render": "clapper",
    "BUTTONS_render": "render",
    "BUTTONS_pause": "pause",
    "BUTTONS_stop": "stop",
    "BUTTONS_reload": "refresh",
    "BUTTONS_cook": "auto",
    "IMAGE_region": "region",
    "IMAGE_rop": "rop",
    "BUTTONS_jump_to_node": "jump",
    "IMAGE_camera": "camera",
    "IMAGE_look": "look",
    "IMAGE_settings": "preset",
    "IMAGE_denoise": "denoise",
    "BUTTONS_resolution_link": "link",
    "IMAGE_aov": "aov",
    "IMAGE_exposure": "exposure",
    "IMAGE_gamma": "gamma",
    "BUTTONS_revert": "reset",
    "IMAGE_checker": "checker",
    "IMAGE_resolution": "res",
    "BUTTONS_resize_mode_space": "keep_scale",
    "IMAGE_zoom": "zoom",
    "IMAGE_zoom_100": "zoom",
    "IMAGE_fit_to_window": "fit",
    "BUTTONS_maximize": "maximize",
    "IMAGE_snapshot_add": "snapshot",
    "IMAGE_diff_compare": "compare",
    "IMAGE_tessellation_update": "tessellation_update",
}
# Single-colour Cycles Tools artwork (config/Icons) without its dark node badge:
# SHOP_material is the project's material icon, the one the Material Preview tool uses.
SHARED_ROOT = ASSET_ROOT.parents[1]
_SHARED = {"SHOP_material": "cycles_material_preview"}
# Controls for which the supplied viewer set has no matching action.
_PATHS = {
    "BUTTONS_help": '<circle cx="12" cy="12" r="9"/><path d="M9.6 9a2.5 2.5 0 0 1 5 .2c0 1.9-2.6 2.2-2.6 4M12 16.7v.1"/>',
    "IMAGE_display": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M12 17v4m-4 0h8"/>',
    "IMAGE_view": '<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 17l9 5 9-5"/>',
    "BUTTONS_lock_closed": '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "BUTTONS_image_color_picker": '<circle cx="12" cy="12" r="6"/><path d="M12 2v5m0 10v5M2 12h5m10 0h5"/>',
    "BUTTONS_save_image": '<path d="M5 3h13l3 3v15H3V3z"/><path d="M7 3v6h10V3M7 21v-8h10v8"/>',
    "BUTTONS_folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v9H3zM3 11h18"/>',
    "BUTTONS_info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.1"/>',
}
_CACHE = {}
_SOURCES = {}


def svg_source(name, color):
    if name in _ASSETS:
        asset = _ASSETS[name]
        if asset not in _SOURCES:
            _SOURCES[asset] = (ASSET_ROOT / (asset + ".svg")).read_text(encoding="utf-8")
        return _SOURCES[asset].replace("#E6E6E6", color)
    if name in _SHARED:
        asset = _SHARED[name]
        if asset not in _SOURCES:
            source = (SHARED_ROOT / (asset + ".svg")).read_text(encoding="utf-8")
            _SOURCES[asset] = re.sub(r"<rect\b[^>]*/>", "", source, count=1)
        return _SOURCES[asset].replace("#ffffff", color)
    if name not in _PATHS:
        return None
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" color="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + _PATHS[name]
        + "</svg>"
    )


def rasterize(svg, size):
    """Uniformly fit the SVG's complete viewBox, including its own padding."""
    size = QtCore.QSize(size, size) if isinstance(size, int) else size
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode()))
    pixmap = QtGui.QPixmap(size)
    pixmap.fill(QtCore.Qt.transparent)
    bounds = renderer.viewBoxF()
    if not renderer.isValid() or bounds.isEmpty():
        return pixmap
    scale = min(size.width() / bounds.width(), size.height() / bounds.height())
    width, height = bounds.width() * scale, bounds.height() * scale
    target = QtCore.QRectF((size.width() - width) / 2, (size.height() - height) / 2, width, height)
    renderer.setAspectRatioMode(QtCore.Qt.KeepAspectRatio)
    painter = QtGui.QPainter(pixmap)
    try:
        renderer.render(painter, target)
    finally:
        painter.end()
    return pixmap


def glyph(name, color=None):
    if color is None:
        from .viewer_theme import read_theme

        color = read_theme()["text"].name()
    key = (name, color)
    if key not in _CACHE:
        svg = svg_source(name, color)
        if svg is None:
            return QtGui.QIcon()
        if len(_CACHE) > 256:
            _CACHE.clear()
        icon = QtGui.QIcon()
        for size in (16, 24, 32, 48, 64):
            icon.addPixmap(rasterize(svg, size))
        _CACHE[key] = icon
    return QtGui.QIcon(_CACHE[key])
