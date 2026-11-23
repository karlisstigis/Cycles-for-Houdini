# SPDX-License-Identifier: Apache-2.0
"""Single-height toolbar rows whose minimum width keeps every group visible."""

from PySide6 import QtCore, QtWidgets


class ToolbarRow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.groups = []
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.row = QtWidgets.QHBoxLayout(self)
        self.row.setContentsMargins(8, 3, 8, 3)
        self.row.setSpacing(8)
        self.row.setAlignment(QtCore.Qt.AlignVCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        self._spring = False
        # Shortest row content in pixels; apply_metrics scales it with the
        # chrome (24 at a 12 px font), so a row never outgrows its controls.
        self.min_content_height = 24

    def required_width(self):
        margins = self.row.contentsMargins()
        widths = [g.minimumSizeHint().width() if stretch else g.sizeHint().width() for g, stretch, _, _ in self.groups]
        return sum(widths) + self.row.spacing() * max(0, len(widths) - 1) + margins.left() + margins.right()

    def minimumSizeHint(self):
        return self.sizeHint()

    def sizeHint(self):
        margins = self.row.contentsMargins()
        heights = [g.sizeHint().height() for g, *_ in self.groups]
        return QtCore.QSize(
            self.required_width(), max(heights + [self.min_content_height]) + margins.top() + margins.bottom()
        )

    def group(self, *widgets, stretch=False, align_right=False, priority=0):
        group = QtWidgets.QWidget(self)
        layout = QtWidgets.QHBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.setAlignment(QtCore.Qt.AlignVCenter)
        group.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding if stretch else QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed
        )
        for widget in widgets:
            layout.addWidget(widget)
            widget.show()
        if align_right and not self._spring:
            self.row.addStretch(1)
            self._spring = True
        self.groups.append((group, stretch, align_right, priority))
        self.row.addWidget(group, int(stretch))
        return group

    def arrange(self):
        for group, *_ in self.groups:
            group.layout().invalidate()
        self.row.invalidate()
        self.updateGeometry()
