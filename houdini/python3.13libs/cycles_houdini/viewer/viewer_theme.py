# SPDX-License-Identifier: Apache-2.0
"""Viewer colors from the active Houdini scheme, with Qt palette fallback."""

try:
    import hou
except ImportError:  # Pure-Qt tests of Cycles panels; read_theme then uses the Qt palette.
    hou = None
from PySide6 import QtGui, QtWidgets


_ROLES = {
    "background": ("BackColor", QtGui.QPalette.Window),
    "toolbar": ("PaneHeaderGradLow", QtGui.QPalette.Window),
    "canvas": ("PaneEmptyBG", QtGui.QPalette.Dark),
    "field": ("TextboxBG", QtGui.QPalette.Base),
    "numeric": ("ButtonGradLow", QtGui.QPalette.Button),
    "dropdown_hi": ("ButtonGradHi", QtGui.QPalette.Button),
    "dropdown_lo": ("ButtonGradLow", QtGui.QPalette.Button),
    "border": ("TextboxBorderPrimary", QtGui.QPalette.Mid),
    "divider": ("BorderDark", QtGui.QPalette.Mid),
    "text": ("TextColor", QtGui.QPalette.Text),
    "muted": ("SecondaryText", QtGui.QPalette.PlaceholderText),
    "disabled": ("MenuTextDisabled", QtGui.QPalette.Disabled),
    "button": ("ButtonGradLow", QtGui.QPalette.Button),
    "hover": ("ButtonGradHi", QtGui.QPalette.Light),
    "hover_border": ("ButtonHoverHighlight", QtGui.QPalette.Light),
    "active": ("ButtonPressedGradLow", QtGui.QPalette.Highlight),
    "active_border": ("ButtonPressedGradHi", QtGui.QPalette.Highlight),
    "active_text": ("ButtonPressedText", QtGui.QPalette.HighlightedText),
    "menu": ("MenuBG", QtGui.QPalette.Base),
    "selection": ("ListEntrySelected", QtGui.QPalette.Highlight),
    "selection_text": ("MenuTextSelected", QtGui.QPalette.HighlightedText),
    "progress": ("ProgressMeterGradLo", QtGui.QPalette.Highlight),
    "warning": ("WarningColor", QtGui.QPalette.BrightText),
    "thumb": ("SliderThumbGradHi", QtGui.QPalette.ButtonText),
}


def read_theme():
    palette = QtWidgets.QApplication.palette()
    result = {}
    for role, (name, fallback) in _ROLES.items():
        # HOM returns plain RGB values. Own the QColor rather than retaining
        # a wrapper around a Houdini-owned Qt pointer across event callbacks.
        if hou is not None and hou.isUIAvailable():
            result[role] = QtGui.QColor.fromRgbF(*hou.ui.colorFromName(name).rgb())
        elif role == "disabled":
            result[role] = palette.color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
        else:
            result[role] = palette.color(fallback)
    # Houdini 22 themes (Edit Theme) paint the native UI from theme roles. Use
    # the same roles the native style uses for each part, so the viewer
    # follows every preset; the legacy scheme names above remain the fallback.
    themed = {role: _theme_role(name) for role, name in _THEME_ROLES.items()}
    for role, value in themed.items():
        if value is not None and role in result:
            result[role] = value
    for role in ("dropdown_hi", "dropdown_lo"):
        if themed["button"] is not None:
            result[role] = QtGui.QColor(themed["button"])
    if themed["button"] is not None:
        result["hover"] = themed["button"].lighter(120)
    pressed = _theme_role("pressed")
    pressed_text = _theme_role("pressedFg")
    if themed["active"] is not None and pressed is not None and pressed_text is not None:
        result["active_border"] = _blend(pressed, pressed_text, 0.33)
    result["pressed"] = pressed if pressed is not None else QtGui.QColor(result["active"])
    result["active_hover"] = result["active"].lighter(110)
    return result


# Viewer role -> Houdini 22 theme role, matching hutil.qt.pluto's own drawing:
# toolbars fill with bg, fields and numeric inputs with field, combo boxes and
# buttons with button, menus with surfaceHighest, menu selection with
# highlight, progress with primary and checked buttons with checkedSurface.
_THEME_ROLES = {
    "background": "bg",
    "toolbar": "bg",
    "canvas": "paneTabEmptyBg",
    "field": "field",
    "numeric": "field",
    "border": "divider",
    "divider": "divider",
    "text": "fg",
    "muted": "dimFg",
    "disabled": "dimmerFg",
    "button": "button",
    "hover_border": "buttonHigh",
    "active": "checkedSurface",
    "active_text": "checkedSurfaceFg",
    "menu": "surfaceHighest",
    "selection": "highlight",
    "selection_text": "highlightFg",
    "progress": "primary",
    "thumb": "buttonHigh",
}


def _theme_role(name):
    app = QtWidgets.QApplication.instance()
    value = app.property("pluto_" + name) if app is not None else None
    # Copy into a Python-owned colour, as for the HOM values above.
    return QtGui.QColor(value) if isinstance(value, QtGui.QColor) and value.isValid() else None


def _blend(first, second, amount):
    return QtGui.QColor.fromRgbF(*(a + (b - a) * amount for a, b in zip(first.getRgbF(), second.getRgbF())))


def colors(widget):
    while widget is not None:
        value = getattr(widget, "_viewer_colors", None)
        if value is not None:
            return value
        widget = widget.parentWidget()
    return read_theme()


def contrast_ratio(first, second):
    def luminance(color):
        rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in color.getRgbF()[:3]]
        return sum(a * b for a, b in zip(rgb, (0.2126, 0.7152, 0.0722)))

    a, b = sorted((luminance(first), luminance(second)))
    return (b + 0.05) / (a + 0.05)


def contrasting(color, background, minimum=3.0):
    """Keep the theme's hue, increasing contrast only when it is unreadable."""
    if contrast_ratio(color, background) >= minimum:
        return color
    end = max((QtGui.QColor("black"), QtGui.QColor("white")), key=lambda c: contrast_ratio(c, background))
    low, high = 0.0, 1.0
    for _ in range(12):
        mid = (low + high) / 2
        if contrast_ratio(_blend(color, end, mid), background) < minimum:
            low = mid
        else:
            high = mid
    return _blend(color, end, high)


def icon_tint(widget, theme, mode=QtGui.QIcon.Normal, state=QtGui.QIcon.Off):
    primary = bool(widget.property("cycles_primary"))
    background = theme["button"] if widget.property("cycles_glyph") == "IMAGE_final_render" else theme["toolbar"]
    parent = widget.parentWidget()
    while parent is not None:
        cluster = parent.property("cycles_cluster")
        if cluster:
            background = theme[
                {"field": "numeric", "dropdown": "button", "transport": "field", "segment": "toolbar"}[cluster]
            ]
            break
        parent = parent.parentWidget()
    foreground = theme["text"]
    if mode == QtGui.QIcon.Disabled:
        foreground = theme["disabled"]
        if primary:
            background = theme["pressed"]
    elif state == QtGui.QIcon.On or primary:
        foreground = theme["active_text"]
        background = theme["active_hover" if mode == QtGui.QIcon.Active else "active"]
    elif mode == QtGui.QIcon.Active:
        background = theme["hover"]
    return contrasting(foreground, background, 2.0 if mode == QtGui.QIcon.Disabled else 3.0)


def apply_theme(panel, theme):
    panel._viewer_colors = theme
    panel.canvas._viewer_colors = theme  # The image-only window reparents this canvas.
    panel.canvas.update_checker(theme["field"], theme["button"])
    tint_glyphs(panel, theme)
    panel.canvas.update()
    if hasattr(panel, "backgrounds_changed"):
        panel.backgrounds_changed()  # New checker or canvas colours: new composites.


def tint_glyphs(root, theme):
    """Re-tint every Cycles glyph button and label below root for theme (shared by all Cycles panels)."""
    from .viewer_icons import glyph

    for widget in root.findChildren(QtWidgets.QWidget):
        name = widget.property("cycles_glyph")
        if isinstance(widget, QtWidgets.QToolButton) and name:
            icon = QtGui.QIcon()
            for mode, state in (
                (QtGui.QIcon.Normal, QtGui.QIcon.Off),
                (QtGui.QIcon.Active, QtGui.QIcon.Off),
                (QtGui.QIcon.Normal, QtGui.QIcon.On),
                (QtGui.QIcon.Active, QtGui.QIcon.On),
                (QtGui.QIcon.Disabled, QtGui.QIcon.Off),
                (QtGui.QIcon.Disabled, QtGui.QIcon.On),
            ):
                source = glyph(name, icon_tint(widget, theme, mode, state).name())
                for size in (16, 24, 32, 48, 64):
                    icon.addPixmap(source.pixmap(size, size), mode, state)
            widget.setIcon(icon)
        if isinstance(widget, QtWidgets.QLabel) and name:
            size = widget.size()
            widget.setPixmap(glyph(name, icon_tint(widget, theme).name()).pixmap(size))
