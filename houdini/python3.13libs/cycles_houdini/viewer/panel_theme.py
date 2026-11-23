# SPDX-License-Identifier: Apache-2.0
"""Houdini theme, monitor scale and the viewer's chrome rules for any Cycles panel.

The render viewer's MonitorScale owns its canvas and toolbars. Other Cycles
panels (the asset browser) use this smaller helper so that every panel reads
the same Houdini theme roles (`viewer_theme.read_theme`), uses the same chrome
stylesheet (`viewer_layout.scoped_style`), the same 12px-font metrics
(`viewer_chrome.apply_dimensions`) and the same tinted glyphs
(`viewer_theme.tint_glyphs`). It follows Houdini's Edit Theme changes, which
replace the application palette, and per-monitor DPI changes.
"""

import sys

from PySide6 import QtCore, QtGui, QtWidgets

from .ui_scale import logical_scale, scaled_style


def _hou():
    host = sys.modules.get("hou")
    if host is None:
        try:
            import hou as host
        except ImportError:
            return None
    try:
        return host if host.isUIAvailable() else None
    except Exception:
        return None


class PanelTheme:
    """Apply the shared Cycles chrome to `panel`, scoped by the boolean property `scope`.

    `extra_style(theme)` returns panel-specific rules written with the same
    `$role` vocabulary (already substituted) and 12px-font pixel sizes; they are
    scaled with the chrome. `on_theme(theme, scale)` runs after each change.
    Call `update()` from a timer; it does nothing unless the theme, font, global
    UI scale or monitor changed.
    """

    def __init__(self, panel, scope, extra_style=None, on_theme=None):
        self.panel = panel
        self.scope = scope
        self.extra_style = extra_style
        self.on_theme = on_theme
        self.factor = 1.0
        self.scale = 1.0
        self.theme = None
        self._key = None
        self._palette_key = None
        panel.setProperty(scope, True)
        self._layouts = []

    def capture_layouts(self):
        """Remember authored margins/spacing (12px-font pixels) before the first scaling."""
        self._layouts = []
        for layout in [self.panel.layout()] + self.panel.findChildren(QtWidgets.QLayout):
            if layout is None:
                continue
            margins = layout.contentsMargins()
            self._layouts.append(
                (layout, (margins.left(), margins.top(), margins.right(), margins.bottom()), layout.spacing())
            )

    def _screen_factor(self, app, host):
        panel = self.panel
        screen = app.screenAt(panel.mapToGlobal(panel.rect().center())) or panel.screen()
        if screen is None or host is None:
            return screen, 1.0
        reference = app.primaryScreen()
        reference_dpi = reference.logicalDotsPerInch() if reference else 96.0
        return screen, logical_scale(screen.logicalDotsPerInch(), reference_dpi)

    def update(self, force=False):
        from .viewer_theme import read_theme

        app = QtWidgets.QApplication.instance()
        if app is None:
            return False
        host = _hou()
        palette_key = app.palette().cacheKey()
        if force or palette_key != self._palette_key or self.theme is None:
            # Houdini's Edit Theme replaces the application palette; re-read then.
            self._palette_key = palette_key
            self.theme = read_theme()
        theme = self.theme
        screen, factor = self._screen_factor(app, host)
        font = QtGui.QFont(app.font())
        if font.pixelSize() > 0:
            font.setPixelSize(max(1, round(font.pixelSize() * factor)))
        elif font.pointSizeF() > 0:
            dpi = screen.logicalDotsPerInch() if screen is not None else 96.0
            font.setPixelSize(max(1, round(font.pointSizeF() * dpi / 72)))
        global_scale = 1.0
        if host is not None:
            try:
                global_scale = float(host.ui.globalScaleFactor())
            except Exception:
                global_scale = 1.0
        key = (
            screen.name() if screen is not None else "",
            screen.devicePixelRatio() if screen is not None else 1.0,
            factor,
            global_scale,
            font.key(),
            palette_key,
            tuple((role, value.rgba()) for role, value in theme.items()),
        )
        if key == self._key and not force:
            return False
        self._key = key
        self.factor = factor
        self.scale = scale = font.pixelSize() / 12
        from .viewer_chrome import ViewerCombo, apply_dimensions
        from .viewer_layout import scoped_style
        from .viewer_theme import tint_glyphs

        base = (host.qt.styleSheet() if host is not None else "") or app.styleSheet() or ""
        chrome = scoped_style(self.scope, theme)
        if self.extra_style is not None:
            chrome += "\n" + self.extra_style(theme)
        selector = f'QWidget[{self.scope}="true"]'
        style = (
            scaled_style(base, factor)
            + "\n"
            + scaled_style(chrome, scale)
            # The application stylesheet may set explicit child fonts; scope the size override here.
            + f"\n{selector}, {selector} QWidget {{ font-size: {font.pixelSize()}px; }}"
        )
        panel = self.panel
        panel._viewer_colors = theme
        panel.setStyleSheet(style)
        panel.setFont(QtGui.QFont(font))
        for layout, margins, spacing in self._layouts:
            layout.setContentsMargins(*(round(value * scale) for value in margins))
            if spacing >= 0:
                layout.setSpacing(round(spacing * scale))
        # Measure after the stylesheet font is polished and glyph icons exist
        # (button widths include the icon); labels are re-tinted at their final size.
        for widget in panel.findChildren(QtWidgets.QWidget):
            widget.ensurePolished()
        tint_glyphs(panel, theme)
        apply_dimensions(panel, scale)
        for combo in panel.findChildren(ViewerCombo):
            combo._place_editor()
        for row in panel.findChildren(QtWidgets.QWidget):
            if hasattr(row, "arrange"):
                row.arrange()
        tint_glyphs(panel, theme)
        if self.on_theme is not None:
            self.on_theme(theme, scale)
        panel.updateGeometry()
        return True


def install(panel, scope, extra_style=None, on_theme=None, interval=500):
    """PanelTheme plus a polling timer owned by panel (cheap when nothing changed)."""
    helper = PanelTheme(panel, scope, extra_style=extra_style, on_theme=on_theme)
    timer = QtCore.QTimer(panel)
    timer.setInterval(interval)
    timer.timeout.connect(lambda: panel.isVisible() and helper.update())
    timer.start()
    helper.timer = timer
    return helper
