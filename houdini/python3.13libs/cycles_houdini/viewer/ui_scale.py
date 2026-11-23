# SPDX-License-Identifier: Apache-2.0
"""Local monitor scaling for Qt controls hosted by Houdini's native windows.

Some host configurations disable Qt's device-pixel scaling and initialize a
global pixel font/theme from one screen. In that case logical DPI still differs
between screens. Scale that theme locally; never change the QApplication or
Windows DPI policy. When Qt already scales per-monitor, logical DPI is normally
constant, so this correction is one and Qt handles the device pixel ratio.
"""

import math
import re

try:
    import hou
except ImportError:  # Pure-Qt tests; MonitorScale is then disabled.
    hou = None
from PySide6 import QtCore, QtGui, QtWidgets


_PIXELS = re.compile(r"(?<![\w./-])(-?\d+(?:\.\d+)?)px\b")


def logical_scale(dpi, reference_dpi):
    if not all(math.isfinite(value) and value > 0 for value in (dpi, reference_dpi)):
        return 1.0
    return dpi / reference_dpi


def scaled_style(style, factor):
    # Only CSS length tokens are changed; asset names and other theme values
    # stay intact. Always rescale the original, never the previous scaled copy.
    return _PIXELS.sub(lambda match: f"{float(match[1]) * factor:g}px", style)


class MonitorScale:
    def __init__(self, panel):
        self.panel = panel
        self.enabled = hou is not None and hou.isUIAvailable()
        self.factor = 1.0
        self._key = None
        self._host_key = None
        self._floating = None
        self._palette_key = None
        self._theme = None
        if not self.enabled:
            return
        app = QtWidgets.QApplication.instance()
        # Houdini can rename the root widget when it embeds a Python Panel.
        panel.setProperty("cycles_render_panel", True)
        reference = app.primaryScreen()
        self.reference_dpi = reference.logicalDotsPerInch() if reference else 96.0
        self._layouts = []
        style = panel.style()
        self._indicator = (
            style.pixelMetric(QtWidgets.QStyle.PM_IndicatorWidth, None, panel.auto),
            style.pixelMetric(QtWidgets.QStyle.PM_IndicatorHeight, None, panel.auto),
        )
        for layout in panel.findChildren(QtWidgets.QLayout):
            margins = layout.contentsMargins()
            self._layouts.append(
                (layout, (margins.left(), margins.top(), margins.right(), margins.bottom()), layout.spacing())
            )

    def _sync_minimum(self):
        window = self.panel.window()
        key = (id(window), id(self.panel.parentWidget()), self.panel.objectName())
        if key != self._host_key:
            self._host_key = key
            self._floating = None
            # Resolve the native owner only on an embedding change, never by
            # rescanning panes during ordinary viewer polling or transforms.
            for floating in hou.ui.floatingPanels():
                if any(
                    pane.type() == hou.paneTabType.PythonPanel and pane.activeInterfaceRootWidget() is self.panel
                    for pane in floating.paneTabs()
                ):
                    self._floating = floating
                    break
        if self._floating is not None:
            # Native fullscreen owns its geometry and saved normal placement.
            # Resizing that window from our minimum-width poll fights its
            # fullscreen/restore transition and can prevent a clean exit.
            if self._floating.isFullscreen():
                return
            native = hou.qt.floatingPanelWindow(self._floating)
            if native is not None:
                if native.isFullScreen() or native.isMaximized():
                    return
                required = self.panel.minimumWidth()
                minimum = required + max(0, native.width() - self.panel.width())
                if native.minimumWidth() != minimum:
                    native.setMinimumWidth(minimum)
                if native.width() < required:
                    # Windows can change the native non-client border after
                    # Qt's DPI update. Correct the measured client shortfall
                    # through Houdini's owning window rather than a child.
                    if QtWidgets.QApplication.mouseButtons() == QtCore.Qt.NoButton:
                        # FloatingPanel.size() returns floats in Houdini 22;
                        # setSize accepts only integers (TypeError otherwise).
                        width, height = self._floating.size()
                        self._floating.setSize((round(width + required - native.width()), round(height)))
                return
        if window == hou.qt.mainWindow() or window.isFullScreen() or window.isMaximized():
            return
        minimum = self.panel.minimumWidth() + max(0, window.width() - self.panel.width())
        if window.minimumWidth() != minimum:
            window.setMinimumWidth(minimum)

    def update(self):
        if not self.enabled or not self.panel.isVisible():
            return
        app = QtWidgets.QApplication.instance()
        # The Python Panel may be embedded in a native window without a Qt
        # QWindow handle. Its QWidget.screen() can remain the primary screen.
        # Resolve the screen from its current global position instead.
        screen = app.screenAt(self.panel.mapToGlobal(self.panel.rect().center())) or self.panel.screen()
        if screen is None:
            return
        font = app.font()
        factor = logical_scale(screen.logicalDotsPerInch(), self.reference_dpi)
        from .viewer_theme import read_theme, apply_theme

        # Houdini pushes a theme change by replacing the application palette
        # (Edit Theme -> setApplicationTheme -> QApplication.setPalette); it
        # does not poll. Re-read the theme only then, not on every monitor poll.
        palette_key = app.palette().cacheKey()
        if palette_key != self._palette_key or self._theme is None:
            self._palette_key = palette_key
            self._theme = read_theme()
        theme = self._theme
        theme_key = tuple((key, value.rgba()) for key, value in theme.items())
        # Windows/Qt can replace QScreen objects after fullscreen transitions or
        # display changes. Retain plain screen metadata, never a native wrapper.
        rect = screen.geometry()
        key = (
            screen.name(),
            rect.x(),
            rect.y(),
            rect.width(),
            rect.height(),
            screen.devicePixelRatio(),
            factor,
            hou.ui.globalScaleFactor(),
            font.key(),
            app.palette().cacheKey(),
            theme_key,
        )
        if font.pixelSize() > 0:
            font.setPixelSize(max(1, round(font.pixelSize() * factor)))
        elif font.pointSizeF() > 0:
            # Native embedding may retain the old screen for point conversion.
            font.setPixelSize(max(1, round(font.pointSizeF() * screen.logicalDotsPerInch() / 72)))
        if key == self._key and self.panel.render_button.font().pixelSize() == font.pixelSize():
            self._sync_minimum()
            self.panel.canvas.display_density_changed()
            return
        self._key = key
        self.factor = factor
        style = hou.qt.styleSheet() or app.styleSheet()
        # Some Houdini builds implement their theme in QStyle rather than QSS.
        # Its indicator metrics are global even after a native window moves.
        # A local size rule retains its native colors/check state drawing.
        from .viewer_layout import redesign_style
        from .viewer_chrome import apply_metrics

        chrome_scale = font.pixelSize() / 12
        style = (
            scaled_style(style, factor)
            + "\n"
            + scaled_style(redesign_style(theme), chrome_scale)
            + f"\nQCheckBox::indicator {{ width: {round(self._indicator[0] * factor)}px; height: {round(self._indicator[1] * factor)}px; }}"
        )
        # The application stylesheet may set explicit child fonts, overriding
        # an inherited QFont. Scope the font override to this panel only.
        style += f'\nQWidget[cycles_render_panel="true"], QWidget[cycles_render_panel="true"] QWidget {{ font-size: {font.pixelSize()}px; }}'
        self.panel.setStyleSheet(style)
        self.panel.setFont(QtGui.QFont(font))
        for layout, margins, spacing in self._layouts:
            layout.setContentsMargins(*(round(value * chrome_scale) for value in margins))
            if spacing >= 0:
                layout.setSpacing(round(spacing * chrome_scale))
        apply_metrics(self.panel, chrome_scale)
        apply_theme(self.panel, theme)
        self._sync_minimum()
        self.panel.update_history_controls()
        self.panel.canvas.setMinimumSize(round(320 * factor), round(180 * factor))
        self.panel.texture_info.setMaximumHeight(round(120 * factor))
        for label in (self.panel.info, self.panel.memory_info):
            label.setFixedHeight(label.fontMetrics().lineSpacing() + max(1, round(8 * factor)))
        self.panel.canvas.display_density_changed()
        self.panel.layout().invalidate()
        self.panel.updateGeometry()
