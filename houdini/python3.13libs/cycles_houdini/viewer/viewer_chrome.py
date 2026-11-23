# SPDX-License-Identifier: Apache-2.0
"""Reference-design dimensions, in 12px-font logical pixels.

All chrome uses one scale, including native Houdini fields. Spare width belongs
to the row's spring, never to path fields or gaps inside a control group.
"""

from PySide6 import QtCore, QtGui, QtWidgets


class ViewerToolButton(QtWidgets.QToolButton):
    """Keep the native themed surface, centre icon content without style offsets."""

    def paintEvent(self, event):
        if self.icon().isNull() or self.toolButtonStyle() == QtCore.Qt.ToolButtonTextOnly:
            return super().paintEvent(event)
        option = QtWidgets.QStyleOptionToolButton()
        self.initStyleOption(option)
        icon = QtGui.QIcon(option.icon)
        text = option.text
        option.icon = QtGui.QIcon()
        option.text = ""
        painter = QtGui.QPainter(self)
        try:
            self.style().drawComplexControl(QtWidgets.QStyle.CC_ToolButton, option, painter, self)
            mode = (
                QtGui.QIcon.Disabled
                if not self.isEnabled()
                else QtGui.QIcon.Active
                if self.underMouse() or self.isDown()
                else QtGui.QIcon.Normal
            )
            state = QtGui.QIcon.On if self.isChecked() else QtGui.QIcon.Off
            size = icon.actualSize(self.iconSize(), mode, state)
            label = text if self.toolButtonStyle() == QtCore.Qt.ToolButtonTextBesideIcon else ""
            gap = round(4 * QtGui.QFontInfo(self.font()).pixelSize() / 12) if label else 0
            width = size.width() + gap + (self.fontMetrics().horizontalAdvance(label) if label else 0)
            left = round((self.width() - width) / 2)
            rect = QtCore.QRect(left, round((self.height() - size.height()) / 2), size.width(), size.height())
            icon.paint(painter, rect, QtCore.Qt.AlignCenter, mode, state)
            if label:
                from .viewer_theme import colors, icon_tint

                painter.setPen(icon_tint(self, colors(self), mode, state))
                painter.drawText(
                    QtCore.QRect(rect.right() + 1 + gap, 0, width - size.width() - gap, self.height()),
                    QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                    label,
                )
        finally:
            painter.end()


class ViewerCombo(QtWidgets.QComboBox):
    """Custom field painting; Qt retains its popup, selection and keyboard APIs.

    Houdini's native combo style reserves an extra text inset even with a custom
    drop-down. Paint the compact field ourselves instead of clipping values.
    """

    def event(self, event):
        if event.type() == QtCore.QEvent.ToolTip:
            text = self.toolTip()
            value = self.currentText()
            if value and value not in text:
                text = value + ("\n" + text if text else "")
            if text:
                QtWidgets.QToolTip.showText(event.globalPos(), text, self)
                event.accept()
                return True
        return super().event(event)

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()

    def showPopup(self):
        # Popup width follows the longest entry rather than the compact field.
        self.view().setTextElideMode(QtCore.Qt.ElideNone)
        width = max(
            [self.width()] + [self.fontMetrics().horizontalAdvance(self.itemText(i)) + 40 for i in range(self.count())]
        )
        screen = QtWidgets.QApplication.screenAt(self.mapToGlobal(self.rect().center())) or self.screen()
        self.view().setMinimumWidth(min(width, screen.availableGeometry().width() - 40))
        self.view().setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        super().showPopup()

    def _place_editor(self):
        if not self.isEditable():
            return
        editor = self.lineEdit()
        scale = self.height() / 22
        width = max(1, self.width() - round(16 * scale))
        # The host style also places the editor later in the event cycle.
        # Constrain its width so that native placement cannot shrink the text
        # area after our resize handler and cut off the first percentage digit.
        editor.setFixedWidth(width)
        editor.setGeometry(0, 0, width, self.height())
        editor.setTextMargins(0, 0, 0, 0)
        editor.setContentsMargins(0, 0, 0, 0)

    def setEditable(self, editable):
        super().setEditable(editable)
        if editable:
            self.lineEdit().setStyleSheet("QLineEdit {padding: 0px; border: none; background: transparent;}")
        self._place_editor()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isEditable():
            QtCore.QTimer.singleShot(0, self._place_editor)

    def changeEvent(self, event):
        super().changeEvent(event)
        if self.isEditable() and event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.StyleChange):
            QtCore.QTimer.singleShot(0, self._place_editor)

    def paintEvent(self, event):
        from .viewer_theme import colors

        theme = colors(self)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        scale = QtGui.QFontInfo(self.font()).pixelSize() / 12
        painter.setPen(theme["text" if self.isEnabled() else "disabled"])
        area = QtCore.QRectF(0, 0, self.width() - 16 * scale, self.height())
        # Qt positions and paints the editable percentage's real line editor.
        # Never change child geometry during painting: that can keep Houdini
        # continuously repainting and prevent its idle render callbacks running.
        if not self.isEditable():
            text = self.fontMetrics().elidedText(self.currentText(), QtCore.Qt.ElideRight, int(area.width()))
            painter.drawText(area, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
        painter.setPen(
            QtGui.QPen(
                theme["text" if self.isEnabled() else "disabled"],
                1.4 * scale,
                QtCore.Qt.SolidLine,
                QtCore.Qt.RoundCap,
                QtCore.Qt.RoundJoin,
            )
        )
        center = QtCore.QPointF(self.width() - 8 * scale, self.height() / 2)
        path = QtGui.QPainterPath(center + QtCore.QPointF(-3 * scale, -1.5 * scale))
        path.lineTo(center + QtCore.QPointF(0, 1.5 * scale))
        path.lineTo(center + QtCore.QPointF(3 * scale, -1.5 * scale))
        painter.drawPath(path)


class HistorySlider(QtWidgets.QSlider):
    """Compact snapshot rail with integer positions and a round grab handle."""

    def __init__(self):
        super().__init__(QtCore.Qt.Horizontal)

    def paintEvent(self, event):
        from .viewer_theme import colors

        theme = colors(self)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        scale = QtGui.QFontInfo(self.font()).pixelSize() / 12
        radius = 7 * scale
        left = radius
        span = max(1, self.width() - 2 * radius)
        y = self.height() / 2
        painter.setPen(QtGui.QPen(theme["border"], 4 * scale, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        painter.drawLine(QtCore.QPointF(left, y), QtCore.QPointF(left + span, y))
        count = max(1, self.maximum() - self.minimum())
        painter.setPen(QtGui.QPen(theme["muted"], scale))
        for value in range(self.minimum(), self.maximum()):
            x = left + span * (value - self.minimum()) / count
            painter.drawLine(QtCore.QPointF(x, y - 5 * scale), QtCore.QPointF(x, y + 5 * scale))
        x = left + span * ((self.value() - self.minimum()) / count if self.maximum() > self.minimum() else 1)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(theme["thumb" if self.isEnabled() else "disabled"])
        painter.drawEllipse(QtCore.QPointF(x, y), radius, radius)

    def _pick(self, event):
        radius = 7 * QtGui.QFontInfo(self.font()).pixelSize() / 12
        fraction = (event.position().x() - radius) / max(1, self.width() - 2 * radius)
        self.setValue(round(self.minimum() + max(0, min(1, fraction)) * (self.maximum() - self.minimum())))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.setFocus()
            self._pick(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.LeftButton:
            self._pick(event)
        else:
            super().mouseMoveEvent(event)


def dimensions(widget, width=None, height=24):
    widget.setProperty("cycles_chrome_height", height)
    if width is not None:
        widget.setProperty("cycles_chrome_width", width)
    return widget


def cluster(*widgets, kind=None, gap=4, compact=False):
    if kind is None:
        kind = "dropdown" if any(isinstance(w, QtWidgets.QComboBox) for w in widgets) else "field"
    frame = QtWidgets.QWidget()
    frame.setProperty("cycles_cluster", kind)
    frame.setProperty("cycles_compact", compact)
    row = QtWidgets.QHBoxLayout(frame)
    row.setContentsMargins(
        *((1, 1, 1, 1) if compact or kind == "segment" else (0, 0, 0, 0) if kind == "transport" else (6, 0, 3, 0))
    )
    row.setSpacing(1 if kind in ("segment", "transport") else gap)
    row.setAlignment(QtCore.Qt.AlignVCenter)
    frame.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    dimensions(frame)
    for widget in widgets:
        widget.setProperty("cycles_embedded", True)
        if compact:
            # Icon buttons are 20 px squares; a labelled icon button (Auto,
            # Denoise) keeps the width of its label at the same height.
            labelled = (
                isinstance(widget, QtWidgets.QToolButton)
                and widget.text()
                and not widget.icon().isNull()
                and widget.toolButtonStyle() != QtCore.Qt.ToolButtonIconOnly
            )
            dimensions(widget, None if labelled else 20, 20)
        elif kind == "segment":
            dimensions(widget, height=20)
        row.addWidget(widget)
        widget.show()
    return frame


def apply_metrics(panel, scale=None):
    if scale is None:
        font = QtGui.QFontInfo(panel.font())
        scale = font.pixelSize() / 12
    panel.chrome_scale = scale
    px = lambda value: max(1, round(value * scale))
    apply_dimensions(panel, scale)
    for field in (panel.exposure, panel.gamma):
        if hasattr(field.input, "setWidth"):
            field.input.setWidth(px(38))
        field.input.setFixedSize(px(38), px(22))
        for line in field.input.findChildren(QtWidgets.QLineEdit):
            line.setFixedHeight(px(22))
            line.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    for combo in panel.findChildren(ViewerCombo):
        combo._place_editor()
    for row in panel.findChildren(QtWidgets.QWidget):
        if hasattr(row, "min_content_height"):
            row.min_content_height = px(24)
        if hasattr(row, "arrange"):
            row.arrange()
    rows = getattr(panel, "all_toolbar_rows", ())
    if rows:
        panel.setMinimumWidth(max(row.required_width() for row in rows))


def apply_dimensions(root, scale):
    """Chrome heights, widths, icon sizes and glyph labels at `scale` (12px-font logical pixels)."""
    px = lambda value: max(1, round(value * scale))
    for widget in root.findChildren(QtWidgets.QWidget):
        height = widget.property("cycles_chrome_height")
        width = widget.property("cycles_chrome_width")
        if height is not None:
            widget.setFixedHeight(px(height))
        if width is not None:
            widget.setFixedWidth(px(width))
        if isinstance(widget, QtWidgets.QToolButton):
            height = height or 24
            widget.setFixedHeight(px(height))
            widget.setIconSize(QtCore.QSize(px(14), px(14)))
            if width is None and not widget.property("cycles_fill_width"):
                text = (
                    widget.text()
                    if widget.icon().isNull() or widget.toolButtonStyle() != QtCore.Qt.ToolButtonIconOnly
                    else ""
                )
                content = widget.fontMetrics().horizontalAdvance(text)
                if not widget.icon().isNull():
                    content += px(14 + (4 if text else 0))
                widget.setFixedWidth(max(px(22 if height == 20 else 24), content + px(10)))
        if isinstance(widget, QtWidgets.QLabel) and widget.property("cycles_glyph"):
            from .viewer_icons import glyph
            from .viewer_theme import colors

            widget.setPixmap(
                glyph(widget.property("cycles_glyph"), colors(widget)["text"].name()).pixmap(px(14), px(14))
            )
