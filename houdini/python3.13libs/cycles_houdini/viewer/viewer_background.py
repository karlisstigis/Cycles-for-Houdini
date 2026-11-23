# SPDX-License-Identifier: Apache-2.0
"""Independent display background toggle and nonmodal color picker."""

from PySide6 import QtCore, QtGui, QtWidgets


class _ColorSwatch(QtWidgets.QToolButton):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setAccessibleName("Choose viewer background color")
        self.setToolTip("Choose the background color. Display only; saved images keep their alpha.")
        self.clicked.connect(owner.choose)

    def paintEvent(self, event):
        super().paintEvent(event)
        from .viewer_theme import colors

        theme = colors(self)
        color = self.owner.panel.canvas.background_color or theme["canvas"]
        rect = QtCore.QRectF(self.rect())
        radius = self.height() * 3 / 20
        path = QtGui.QPainterPath()
        path.setFillRule(QtCore.Qt.WindingFill)
        path.addRoundedRect(rect, radius, radius)
        # The left edge meets BG; only the group's outer corners are rounded.
        path.addRect(QtCore.QRectF(0, 0, radius, self.height()))
        path = path.simplified()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillPath(path, color)


class BackgroundColorButton(QtWidgets.QToolButton):
    colorChanged = QtCore.Signal()

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.dialog = None
        self.setText("BG")
        self.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.setCheckable(True)
        self.setProperty("cycles_view_toggle", True)
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setAccessibleName("Toggle colored viewer background")
        self.setToolTip("Toggle the saved background color. Click again for the default background.")
        self.swatch = _ColorSwatch(self)
        self.toggled.connect(self._toggled)
        panel.checker.toggled.connect(self._checker_toggled)

    def _toggled(self, enabled):
        self.panel.canvas.background_enabled = enabled
        if enabled:
            self.panel.checker.setChecked(False)
        self.panel.canvas.update()
        self.panel.backgrounds_changed()

    def _checker_toggled(self, enabled):
        # Both controls may be off: that is the default themed background.
        if enabled:
            self.setChecked(False)

    def set_color(self, color):
        color = QtGui.QColor(color)
        if not color.isValid():
            return
        color.setAlpha(255)
        self.panel.canvas.background_color = color
        self.panel.canvas.update()
        self.panel.backgrounds_changed()
        self.swatch.update()
        self.colorChanged.emit()

    def choose(self, _checked=False):
        if self.dialog is not None:
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        from .viewer_theme import colors

        original = self.panel.canvas.background_color
        original = QtGui.QColor(original) if original is not None else None
        checker = self.panel.checker.isChecked()
        background = self.isChecked()
        dialog = QtWidgets.QColorDialog(original or colors(self)["canvas"], self.window())
        dialog.setWindowTitle("Viewer Background Color")
        dialog.setOption(QtWidgets.QColorDialog.DontUseNativeDialog, True)
        dialog.setWindowModality(QtCore.Qt.NonModal)
        self.dialog = dialog
        self.setChecked(True)
        dialog.currentColorChanged.connect(self.set_color)

        def finished(result):
            if result == QtWidgets.QDialog.Accepted:
                self.set_color(dialog.selectedColor())
                self.setChecked(True)
            else:
                self.panel.canvas.background_color = original
                self.setChecked(background)
                self.panel.checker.setChecked(checker)
                self.panel.canvas.update()
                self.panel.backgrounds_changed()
                self.swatch.update()
                self.colorChanged.emit()
            self.dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def cancel_picker(self):
        if self.dialog is not None:
            self.dialog.reject()
