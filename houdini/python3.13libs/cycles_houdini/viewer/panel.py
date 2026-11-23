# SPDX-License-Identifier: Apache-2.0
"""A dedicated native render panel consuming the same session as Houdini IPR."""

import math
import shiboken6
import time
import weakref
import hou
from PySide6 import QtCore, QtGui, QtWidgets
from .. import color_policy, process_memory
from ..rop.passes import label as pass_label
from ..scene import native_scene
from ..ipr import registry, session_log
from .ui_scale import MonitorScale
from .viewer_chrome import ViewerToolButton
from ..ipr.display_queue import DisplayQueue
from .viewer_wakeup import ViewerWakeup
from . import viewer_lifetime
from .update_clock import UpdateClock

# Status counters (time, progress, samples) refresh rate while state is unchanged.
STATUS_REFRESH_SECONDS = 0.1
# The ROP handle is verified at most this often. Deletion, renaming and scene
# replacement are reported by Houdini events at once; this is only a safety net.
ROP_RECHECK_SECONDS = 1.0

# Every viewer created in this session, for Copy diagnostics (open, hidden, released).
viewers = weakref.WeakSet()


def _rop_events():
    return (hou.nodeEventType.BeingDeleted, hou.nodeEventType.NameChanged)


def _combo_box():
    from .viewer_chrome import ViewerCombo

    return ViewerCombo()


class DisplayFloat(QtWidgets.QWidget):
    """Houdini numeric field with the host's middle-mouse value ladder."""

    valueChanged = QtCore.Signal(float)

    def __init__(self, minimum, maximum, value):
        super().__init__()
        self.minimum, self.maximum = minimum, maximum
        self.default = value
        self._changing = False
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        if hou.isUIAvailable():
            self.input = hou.qt.InputField(hou.qt.InputField.FloatType, 1)
            self.validator = QtGui.QDoubleValidator(minimum, maximum, 2, self.input)
            self.validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
            self.input.setValidator(self.validator)
            self.input.setWidth(76)
        else:
            self.input = QtWidgets.QDoubleSpinBox()
            self.input.setRange(minimum, maximum)
            self.input.setDecimals(2)
            self.input.setSingleStep(0.1)
            self.input.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.input.setProperty("cycles_numeric", True)
        row.addWidget(self.input)
        self.setValue(value)
        self.input.valueChanged.connect(self._changed)
        for widget in (self.input, *self.input.findChildren(QtWidgets.QWidget)):
            widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease):
            if event.button() == QtCore.Qt.MiddleButton and event.modifiers() & QtCore.Qt.ControlModifier:
                if event.type() == QtCore.QEvent.MouseButtonPress:
                    self.setValue(self.default)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def value(self):
        return float(self.input.value())

    def setValue(self, value):
        self.input.setValue(round(float(min(self.maximum, max(self.minimum, value))), 2))

    def _changed(self, *args):
        if self._changing:
            return
        self._changing = True
        try:
            value = round(min(self.maximum, max(self.minimum, self.value())), 2)
            if value != self.value():
                self.setValue(value)
            self.valueChanged.emit(value)
        finally:
            self._changing = False


class DragLabel(QtWidgets.QLabel):
    """Horizontal label dragging leaves the native field's text editing intact."""

    def __init__(self, text, field, step):
        super().__init__(text)
        self.field, self.step, self._drag = field, step, None
        self.setProperty("field_label", True)
        self.setCursor(QtCore.Qt.SizeHorCursor)
        self.setToolTip(
            "Drag left/right to adjust. Ctrl+middle-click resets to default. Hold Shift for finer changes. You can also type in the native field or use its middle-mouse ladder."
        )

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MiddleButton and event.modifiers() & QtCore.Qt.ControlModifier:
            self.field.setValue(self.field.default)
            event.accept()
            return
        if event.button() == QtCore.Qt.LeftButton:
            self._drag = (event.globalPosition().x(), self.field.value())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & QtCore.Qt.LeftButton:
            factor = 0.1 if event.modifiers() & QtCore.Qt.ShiftModifier else 1
            self.field.setValue(self._drag[1] + (event.globalPosition().x() - self._drag[0]) * self.step * factor)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._drag is not None:
            self._drag = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def focusOutEvent(self, event):
        self._drag = None
        super().focusOutEvent(event)


class RenderControls(QtWidgets.QWidget):
    """Compact native-sized buttons on one row."""

    def __init__(self, buttons):
        super().__init__()
        self.buttons = buttons
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        for button in buttons:
            row.addWidget(button)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)


class StatusLine(QtWidgets.QLabel):
    """Keep diagnostics readable without changing the image viewport's geometry."""

    textChanged = QtCore.Signal(str)

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._diagnostic_text = text
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(self.fontMetrics().lineSpacing() + 8)
        self.setToolTip(text)

    def text(self):
        return self._diagnostic_text if self.property("cycles_status_headline") else super().text()

    def setText(self, text, *, tooltip=None):
        tooltip = text if tooltip is None else tooltip
        key = (text, tooltip, bool(self.property("cycles_status_headline")))
        if key == getattr(self, "_text_key", None):
            return
        self._text_key = key
        self._diagnostic_text = text
        # Give Qt the same compact text that we paint, so its native font/DPI
        # sizing follows the visible headline rather than the full diagnostic.
        if self.property("cycles_status_headline"):
            from .viewer_timing import summary

            super().setText(summary(text))
        else:
            super().setText(text)
        self.setToolTip(tooltip)
        self.textChanged.emit(text)

    def minimumSizeHint(self):
        # A non-wrapping QLabel's minimum is its whole text. A long stage text ("Loading render kernels ...")
        # must not widen the viewer: below its full width it is elided at the right (paintEvent).
        hint = super().minimumSizeHint()
        if self.property("cycles_status_headline"):
            from .viewer_timing import summary

            head = summary(self.text())[:48]
            hint.setWidth(min(hint.width(), self.fontMetrics().horizontalAdvance(head) + 8))
        return hint

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.ApplicationFontChange, QtCore.QEvent.StyleChange):
            self.setFixedHeight(self.fontMetrics().lineSpacing() + 8)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        self.style().drawPrimitive(QtWidgets.QStyle.PE_Widget, QtWidgets.QStyleOption(), painter, self)
        painter.setPen(self.palette().color(QtGui.QPalette.WindowText))
        text = self.text()
        if self.property("cycles_status_headline"):
            from .viewer_timing import summary

            text = summary(text)
        text = self.fontMetrics().elidedText(" ".join(text.splitlines()), QtCore.Qt.ElideRight, self.width())
        painter.drawText(self.contentsRect(), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)


class CanvasBehaviour:
    """View state, navigation and overlays shared by every image canvas.

    Mixed into a QWidget (classic and native display) or a QOpenGLWidget
    (GPU display, viewer_display.GpuImageCanvas). Only drawing the image
    itself differs: see draw_image.

    Paint hook, the same for every canvas: each canvas class defines
    paintEvent in Python and has finished painting the view when it returns,
    so wrapping type(canvas).paintEvent observes every completed paint;
    frame_metadata then describes the frame shown. framePainted(generation)
    is emitted at the end of each paint that shows an image.
    """

    display_mode = "classic"

    def note_damage(self, before_key, after_key, rect):
        """The image with cacheKey before_key became after_key by changing rect (x, y, w, h)."""

    def release_display(self):
        """Release display resources (GPU canvas); the CPU images stay with the caller."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        # The canvas paints every pixel, including transparent-image backgrounds.
        # Native fullscreen embedding must not erase/copy the parent underneath
        # each progressive repaint, or inherit a hidden native viewport cursor.
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self.setCursor(QtCore.Qt.ArrowCursor)
        self.image = QtGui.QImage()
        self.full_size = (0, 0)
        self.compare_image = QtGui.QImage()
        self.compare_fraction = 0.5
        self.generation = 0
        self.region_enabled = False
        self.drag_start = None
        self.drag_end = None
        self.focus_enabled = False
        self.view_mode = "fit"
        self._scale = 1.0
        self._pan = QtCore.QPointF()
        self.keep_image_size = False
        self._reference_height = 0
        self.linked_reference = None
        self._display_ratio = self.devicePixelRatioF()
        self._navigation = None
        self._last_position = None
        self._zoom_anchor = None
        self._space_down = False
        self.checker_enabled = True
        self.background_color = QtGui.QColor(0, 0, 0)
        self.background_enabled = False
        # Opaque composites made in scene-linear light (see draw_composites), by
        # the cacheKey of the straight image they belong to: (backgrounds, images).
        self.composites = {}
        self._composite_layer = None
        mask = QtGui.QImage(16, 16, QtGui.QImage.Format_ARGB32_Premultiplied)
        mask.fill(0)
        painter = QtGui.QPainter(mask)
        painter.fillRect(0, 0, 8, 8, QtGui.QColor(255, 255, 255))
        painter.fillRect(8, 8, 8, 8, QtGui.QColor(255, 255, 255))
        painter.end()
        self._checker_mask = QtGui.QBrush(QtGui.QPixmap.fromImage(mask))  # The light cells of _checker.
        self.update_checker(QtGui.QColor(48, 48, 48), QtGui.QColor(64, 64, 64))

    def set_focus_enabled(self, enabled):
        self.focus_enabled = bool(enabled)
        self.update_navigation_cursor()

    def update_navigation_cursor(self):
        if self._navigation is not None:
            return
        shape = (
            QtCore.Qt.OpenHandCursor
            if self._space_down
            else QtCore.Qt.CrossCursor
            if self.focus_enabled
            else QtCore.Qt.ArrowCursor
        )
        if not self.testAttribute(QtCore.Qt.WA_SetCursor) or self.cursor().shape() != shape:
            self.setCursor(shape)

    def update_checker(self, dark, light):
        self._checker_colors = tuple(
            (color.red(), color.green(), color.blue()) for color in (QtGui.QColor(dark), QtGui.QColor(light))
        )
        image = QtGui.QImage(16, 16, QtGui.QImage.Format_RGB32)
        image.fill(dark)
        painter = QtGui.QPainter(image)
        painter.fillRect(0, 0, 8, 8, light)
        painter.fillRect(8, 8, 8, 8, light)
        painter.end()
        self._checker = QtGui.QBrush(QtGui.QPixmap.fromImage(image))

    def set_frame(self, frame):
        self.generation = frame["generation"]
        self.image = self.merge_frame(self.image, frame)
        self.full_size = frame["full_width"], frame["full_height"]
        self.update()
        self.viewChanged.emit()

    @staticmethod
    def logical_size(image):
        size = image.text("cycles:logical_size")
        return tuple(map(int, size.split())) if size else image.size().toTuple()

    @staticmethod
    def full_resolution_copy(image):
        """Copy the displayed pixels at the logical raster size for output or region merging."""
        width, height = image.size().toTuple()
        size = CanvasBehaviour.logical_size(image)
        if (width, height) == size:
            return image.copy()
        from .. import _native

        # Use the renderer's bottom-up nearest-pixel rule, also for odd sizes.
        rgba = _native.expand_display_rgba(bytes(image.constBits()), width, height, *size)
        expanded = QtGui.QImage(rgba, *size, size[0] * 4, QtGui.QImage.Format_RGBA8888).copy()
        expanded.setText("cycles:pixel_aspect", image.text("cycles:pixel_aspect"))
        return expanded

    @staticmethod
    def merge_frame(image, frame, fill=None):
        size = frame["full_width"], frame["full_height"]
        width, height = frame.get("buffer_width", frame["width"]), frame.get("buffer_height", frame["height"])
        tile = QtGui.QImage(frame["rgba"], width, height, width * 4, QtGui.QImage.Format_RGBA8888)
        if "buffer_width" in frame:
            # This storage already covers the whole logical image. The canvas
            # stretches it into its existing image rect when it paints.
            image = tile.copy()
            image.setText("cycles:logical_size", f"{size[0]} {size[1]}")
        else:
            previous_size = None if image is None or image.isNull() else CanvasBehaviour.logical_size(image)
            if previous_size != size:
                image = QtGui.QImage(*size, QtGui.QImage.Format_RGBA8888)
                image.fill(QtGui.QColor(28, 28, 28) if fill is None else fill)
            elif image.size().toTuple() != size:
                # A region update keeps the previous image outside the region.
                image = CanvasBehaviour.full_resolution_copy(image) if (width, height) != size else tile.copy()
            painter = QtGui.QPainter(image)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            painter.drawImage(frame["x"], size[1] - frame["y"] - height, tile)
            painter.end()
        image.setText("cycles:pixel_aspect", str(frame.get("pixel_aspect", 1.0)))
        return image

    def pixel_aspect(self):
        if self.linked_reference:
            return self.linked_reference[2]
        return float(self.image.text("cycles:pixel_aspect") or "1")

    def view_scale(self):
        w, h = self.full_size
        w *= self.pixel_aspect()
        if self.view_mode == "fit":
            if self.linked_reference:
                rw, rh, pa = self.linked_reference
                return min(self.width() / (rw * pa), self.height() / rh) * self.resolution_scale()
            return min(self.width() / w, self.height() / h) if w and h else 1.0
        if self.view_mode == "actual":
            # Qt positions use logical pixels. One render pixel maps to one
            # physical display pixel, including Windows display scaling.
            return self.resolution_scale() / self.devicePixelRatioF()
        return self._scale * self.resolution_scale()

    def resolution_scale(self):
        height = self.full_size[1]
        if self.linked_reference:
            return self.linked_reference[1] / height if height else 1.0
        if not self.keep_image_size or not height:
            return 1.0
        if not self._reference_height:
            self._reference_height = height
        return self._reference_height / height

    def set_linked_reference(self, reference):
        if reference == self.linked_reference:
            return
        scale = self.view_scale()
        has_image = bool(self.full_size[1])
        self.linked_reference = reference
        if has_image and self.view_mode != "fit":
            self.view_mode = "manual"
            self._scale = scale / self.resolution_scale()
        self.update()
        self.viewChanged.emit()

    def zoom_percent(self):
        if self.view_mode == "actual":
            return 100.0
        if self.view_mode == "manual":
            return self._scale * self.devicePixelRatioF() * 100
        if self.linked_reference:
            w, h, pa = self.linked_reference
            return min(self.width() / (w * pa), self.height() / h) * self.devicePixelRatioF() * 100
        return self.view_scale() / self.resolution_scale() * self.devicePixelRatioF() * 100

    def set_keep_image_size(self, enabled):
        # Anchor to the image visible when enabled, or the first image received.
        # A single uniform factor preserves pixel aspect and never stretches pixels.
        scale = self.view_scale()
        self.keep_image_size = enabled
        self._reference_height = self.full_size[1] if enabled else 0
        if self.view_mode != "fit":
            self.view_mode = "manual"
            self._scale = scale / self.resolution_scale()
        self.update()
        self.viewChanged.emit()

    def fit_image(self, *_):
        self.view_mode = "fit"
        self._pan = QtCore.QPointF()
        self.update()
        self.viewChanged.emit()

    def actual_size(self, *_):
        self.view_mode = "actual"
        self._pan = QtCore.QPointF()
        self.update()
        self.viewChanged.emit()

    def set_zoom(self, percent):
        # The selection also works before the first frame arrives.
        self.view_mode = "actual" if percent == 100 else "manual"
        self._scale = percent / (100 * self.devicePixelRatioF())
        self._pan = QtCore.QPointF()
        self.update()
        self.viewChanged.emit()

    def display_density_changed(self):
        ratio = self.devicePixelRatioF()
        if ratio == self._display_ratio:
            return
        # A chosen image zoom denotes physical pixels, independently of
        # UI scale. Retain that zoom across a Qt device-density change.
        change = self._display_ratio / ratio
        if self.view_mode == "manual":
            self._scale *= change
            self._pan *= change
        self._display_ratio = ratio
        self.update()
        self.viewChanged.emit()

    def zoom_at(self, factor, anchor):
        if self.image.isNull() or not math.isfinite(factor) or factor <= 0:
            return
        target = self.image_rect()
        old_scale = self.view_scale()
        scale = min(256.0 / self.devicePixelRatioF(), max(1.0 / 65536.0, old_scale * factor))
        source = (anchor - target.topLeft()) / old_scale
        w, h = self.full_size
        w *= self.pixel_aspect()
        centered = QtCore.QPointF((self.width() - w * scale) / 2, (self.height() - h * scale) / 2)
        self._pan = anchor - source * scale - centered
        self._scale = scale / self.resolution_scale()
        self.view_mode = "manual"
        self.update()
        self.viewChanged.emit()

    def image_rect(self):
        w, h = self.full_size
        w *= self.pixel_aspect()
        if not w or not h:
            return QtCore.QRectF()
        scale = self.view_scale()
        return QtCore.QRectF(
            (self.width() - w * scale) / 2 + self._pan.x(),
            (self.height() - h * scale) / 2 + self._pan.y(),
            w * scale,
            h * scale,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewChanged.emit()

    def draw_image(self, painter, target, image):
        """Draw image scaled into target (logical coordinates), nearest sampling."""
        ratio = painter.device().devicePixelRatioF()
        if ratio == 1:
            from .viewer_display import compact_sample_rect

            rect = target.getRect()
            sample_rect = compact_sample_rect(rect, image.size().toTuple(), self.logical_size(image), ratio)
            if sample_rect != rect:
                target = QtCore.QRectF(*sample_rect)
        painter.drawImage(target, image)

    def display_backgrounds(self):
        """The colours that can be under an image pixel: the two checker colours (dark, light) or one."""
        if self.checker_enabled:
            return self._checker_colors
        background = self.scene_colors()[1]
        return ((background.red(), background.green(), background.blue()),)

    def composite_images(self, image):
        """The composites of image made over the current backgrounds, or None (display-space blend)."""
        entry = self.composites.get(image.cacheKey()) if not image.isNull() else None
        if entry is None or entry[0] != self.display_backgrounds():
            return None
        if any(composite.size() != image.size() for composite in entry[1]):
            return None
        return entry[1]

    def draw_composites(self, painter, target, image):
        """Draw image's scene-linear composites over the straight image just drawn.

        Composites are opaque where converted and transparent elsewhere, so
        pixels outside the converted region keep the straight image's blend.
        With the checker, the second composite (light colour) is drawn only on
        the light cells, through the same texture-brush placement as _checker.
        """
        composites = self.composite_images(image)
        if composites is None:
            return
        self.draw_image(painter, target, composites[0])
        if len(composites) < 2:
            return
        device = painter.device()
        ratio = device.devicePixelRatioF()
        size = (round(device.width() * ratio), round(device.height() * ratio))
        layer = self._composite_layer
        if layer is None or (layer.width(), layer.height()) != size or layer.devicePixelRatioF() != ratio:
            layer = self._composite_layer = QtGui.QImage(*size, QtGui.QImage.Format_ARGB32_Premultiplied)
            layer.setDevicePixelRatio(ratio)
        layer.fill(0)
        masked = QtGui.QPainter(layer)
        masked.fillRect(target, self._checker_mask)
        masked.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
        self.draw_image(masked, target, composites[1])
        masked.end()
        painter.drawImage(QtCore.QPointF(0, 0), layer)

    def scene_colors(self):
        """The theme and the colour under the image (background or canvas)."""
        from .viewer_theme import colors

        theme = colors(self)
        background = (
            self.background_color if self.background_enabled and self.background_color is not None else theme["canvas"]
        )
        return theme, background

    def compare_geometry(self):
        """(target, clip, split x) of the A/B compare wipe, or None."""
        if self.compare_image.isNull() or self.image.isNull():
            return None
        target = self.image_rect()
        clip = QtCore.QRectF(target.left(), target.top(), target.width() * self.compare_fraction, target.height())
        return target, clip, target.left() + target.width() * self.compare_fraction

    def paint_scene(self, painter):
        theme, background = self.scene_colors()
        painter.fillRect(self.rect(), theme["canvas"])
        if self.image.isNull():
            painter.setPen(theme["text"])
            painter.drawText(self.rect(), QtCore.Qt.AlignCenter, "Select a Cycles ROP and render")
        else:
            painter.fillRect(self.image_rect(), background)
            if self.checker_enabled:
                painter.fillRect(self.image_rect(), self._checker)
            self.draw_image(painter, self.image_rect(), self.image)
            self.draw_composites(painter, self.image_rect(), self.image)
        wipe = self.compare_geometry()
        if wipe is not None:
            target, clip, _ = wipe
            painter.save()
            painter.setClipRect(clip)
            painter.fillRect(target, background)
            if self.checker_enabled:
                painter.fillRect(target, self._checker)
            self.draw_image(painter, target, self.compare_image)
            self.draw_composites(painter, target, self.compare_image)
            painter.restore()
        self.paint_overlays(painter, theme)

    def paint_overlays(self, painter, theme):
        """The compare split handle and the region or zoom rectangle being dragged."""
        wipe = self.compare_geometry()
        if wipe is not None:
            target, _, split = wipe
            painter.setPen(QtGui.QPen(theme["active"], 1))
            painter.drawLine(QtCore.QPointF(split, target.top()), QtCore.QPointF(split, target.bottom()))
            painter.fillRect(QtCore.QRectF(split - 3, target.center().y() - 13, 6, 26), theme["active"])
        if self.drag_start is not None and self.drag_end is not None:
            painter.setPen(QtGui.QPen(theme["selection"], 2))
            painter.drawRect(QtCore.QRectF(self.drag_start, self.drag_end).normalized())

    def mousePressEvent(self, event):
        if self.image.isNull():
            return super().mousePressEvent(event)
        self.setFocus(QtCore.Qt.MouseFocusReason)
        position = event.position()
        modifiers = event.modifiers()
        if (
            not self.compare_image.isNull()
            and event.button() == QtCore.Qt.LeftButton
            and not modifiers
            and abs(position.x() - (self.image_rect().left() + self.image_rect().width() * self.compare_fraction)) < 10
        ):
            self._navigation = "compare"
            event.accept()
            return
        if self.focus_enabled and event.button() == QtCore.Qt.LeftButton and not modifiers and not self._space_down:
            pixel = self.pixel_at(position)
            if pixel is not None:
                self.focusPicked.emit(*pixel)
            event.accept()
            return
        if event.button() == QtCore.Qt.MiddleButton or (self._space_down and event.button() == QtCore.Qt.LeftButton):
            self._navigation = "pan"
            self._last_position = position
            self.setCursor(QtCore.Qt.ClosedHandCursor)
        elif event.button() == QtCore.Qt.RightButton:
            if modifiers & QtCore.Qt.ShiftModifier:
                self.regionCleared.emit()
            elif modifiers & QtCore.Qt.ControlModifier:
                self._navigation = "box"
                self.drag_start = position
                self.drag_end = position
            else:
                self._navigation = "zoom"
                self._last_position = position
                self._zoom_anchor = position
                self.setCursor(QtCore.Qt.SizeVerCursor)
        elif event.button() == QtCore.Qt.LeftButton and (self.region_enabled or modifiers & QtCore.Qt.ShiftModifier):
            if self.image_rect().contains(position):
                self._navigation = "region"
                self.drag_start = position
                self.drag_end = position
            elif modifiers & QtCore.Qt.ShiftModifier:
                self.regionCleared.emit()
        elif event.button() == QtCore.Qt.LeftButton and modifiers == QtCore.Qt.ControlModifier:
            # Ctrl+click selects the object under the pointer (double-click: its material).
            pixel = self.pixel_at(position)
            if pixel is not None:
                self.nodePicked.emit(*pixel, "object")
        elif event.button() == QtCore.Qt.LeftButton and not modifiers and self.image_rect().contains(position):
            # Hold the left button to read the pixel values under the pointer.
            self._navigation = "inspect"
            self.setCursor(QtCore.Qt.CrossCursor)
            self.inspect(position)
        else:
            return super().mousePressEvent(event)
        event.accept()
        self.update()

    def pixel_at(self, position):
        """Full-raster pixel (x, y from the bottom) under a widget position, or None outside the image."""
        rect = self.image_rect()
        if not rect.contains(position) or not self.full_size[0] or not self.full_size[1]:
            return None
        x = int((position.x() - rect.left()) / rect.width() * self.full_size[0])
        y = self.full_size[1] - 1 - int((position.y() - rect.top()) / rect.height() * self.full_size[1])
        return min(max(x, 0), self.full_size[0] - 1), min(max(y, 0), self.full_size[1] - 1)

    def inspect(self, position):
        pixel = self.pixel_at(position)
        if pixel is not None:
            self.pixelInspected.emit(*pixel)

    def mouseDoubleClickEvent(self, event):
        if (
            not self.image.isNull()
            and event.button() == QtCore.Qt.LeftButton
            and event.modifiers() == QtCore.Qt.ControlModifier
            and not self.region_enabled
        ):
            pixel = self.pixel_at(event.position())
            if pixel is not None:
                self.nodePicked.emit(*pixel, "material")
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._navigation == "compare":
            self.compare_fraction = max(
                0, min(1, (event.position().x() - self.image_rect().left()) / self.image_rect().width())
            )
            self.update()
        elif self._navigation == "pan":
            self._scale = self.view_scale() / self.resolution_scale()
            self.view_mode = "manual"
            self._pan += event.position() - self._last_position
            self._last_position = event.position()
            self.update()
            self.viewChanged.emit()
        elif self._navigation == "zoom":
            delta = event.position() - self._last_position
            # Match the image viewer's right-drag interaction in either axis.
            self.zoom_at(math.exp(max(-4, min(4, (delta.x() - delta.y()) * 0.01))), self._zoom_anchor)
            self._last_position = event.position()
        elif self._navigation == "inspect":
            self.inspect(event.position())
        elif self.drag_start is not None:
            self.drag_end = event.position()
            self.update()
        else:
            return super().mouseMoveEvent(event)
        event.accept()

    def mouseReleaseEvent(self, event):
        navigation = self._navigation
        if navigation is None:
            return super().mouseReleaseEvent(event)
        # Deliver the last drag position even if Qt coalesced mouse moves.
        self.mouseMoveEvent(event)
        self._navigation = None
        self.update_navigation_cursor()
        if navigation == "inspect":
            self.inspectionEnded.emit()
        if self.drag_start is None:
            event.accept()
            return
        rect = (
            QtCore.QRectF(self.drag_start, event.position())
            .normalized()
            .intersected(self.image_rect())
            .intersected(QtCore.QRectF(self.rect()))
        )
        target = self.image_rect()
        w, h = self.full_size
        self.drag_start = self.drag_end = None
        self.update()
        if rect.width() < 2 or rect.height() < 2:
            return
        if navigation == "box":
            source_center = (rect.center() - target.topLeft()) / self.view_scale()
            scale = min(
                256.0 / self.devicePixelRatioF(),
                self.view_scale() * min(self.width() / rect.width(), self.height() / rect.height()),
            )
            self._scale = scale / self.resolution_scale()
            self.view_mode = "manual"
            self._pan = QtCore.QPointF(
                (w * self.pixel_aspect() / 2 - source_center.x()) * scale, (h / 2 - source_center.y()) * scale
            )
            self.update()
            self.viewChanged.emit()
            event.accept()
            return
        # Convert directly through the displayed pixel scale. A normalized
        # coordinate round trip can turn an exact integer edge into n+epsilon
        # and accidentally expand the selected crop by a pixel.
        scale = self.view_scale()

        def pixel_edge(value, rounding):
            nearest = round(value)
            return nearest if abs(value - nearest) < 1e-7 else rounding(value)

        left = max(0, pixel_edge((rect.left() - target.left()) / (scale * self.pixel_aspect()), math.floor))
        right = min(w, pixel_edge((rect.right() - target.left()) / (scale * self.pixel_aspect()), math.ceil))
        bottom = max(0, h - pixel_edge((rect.bottom() - target.top()) / scale, math.ceil))
        top = min(h, h - pixel_edge((rect.top() - target.top()) / scale, math.floor))
        if right > left and top > bottom:
            self.regionSelected.emit((left, bottom, right - left, top - bottom))
        event.accept()

    def wheelEvent(self, event):
        if self.image.isNull():
            return super().wheelEvent(event)
        delta = event.angleDelta().y()
        if not delta:
            delta = event.pixelDelta().y() * 3
        self.zoom_at(math.exp(max(-4, min(4, delta * math.log(1.2) / 120))), event.position())
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Space:
            self._space_down = True
            self.update_navigation_cursor()
            event.accept()
        elif event.key() == QtCore.Qt.Key_Home or (
            event.key() == QtCore.Qt.Key_F and event.modifiers() & QtCore.Qt.ShiftModifier
        ):
            self.fit_image()
            event.accept()
        elif event.key() == QtCore.Qt.Key_F:
            self.actual_size()
            event.accept()
        elif event.key() == QtCore.Qt.Key_Escape:
            if self._navigation == "inspect":
                self.inspectionEnded.emit()
            self._navigation = None
            self.drag_start = self.drag_end = None
            if self.focus_enabled:
                self.focusCancelled.emit()
            self.update_navigation_cursor()
            self.update()
            event.accept()
            self.fullscreenExitRequested.emit()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == QtCore.Qt.Key_Space:
            self._space_down = False
            self.update_navigation_cursor()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._space_down = False
        if self._navigation == "inspect":
            # A popup or another window took the mouse: the release may never arrive.
            self._navigation = None
            self.inspectionEnded.emit()
        self.update_navigation_cursor()
        super().focusOutEvent(event)


class ImageCanvas(CanvasBehaviour, QtWidgets.QWidget):
    """Classic canvas: QPainter scales the QImage on the CPU."""

    regionSelected = QtCore.Signal(tuple)
    regionCleared = QtCore.Signal()
    viewChanged = QtCore.Signal()
    fullscreenExitRequested = QtCore.Signal()
    framePainted = QtCore.Signal(object)
    focusPicked = QtCore.Signal(int, int)
    focusCancelled = QtCore.Signal()
    nodePicked = QtCore.Signal(int, int, str)
    pixelInspected = QtCore.Signal(int, int)
    inspectionEnded = QtCore.Signal()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        self.paint_scene(painter)
        painter.end()
        if not self.image.isNull():
            self.framePainted.emit(self.generation)


class RenderPanel(QtWidgets.QWidget):
    @property
    def _rop(self):
        # Opening or clearing a scene deletes the ROP before this panel is
        # rebound. Widget callbacks (for example a resolution field losing
        # focus to the file dialog) must then see "no ROP", never a dead handle.
        # Deletion and scene replacement clear the node at once (_rop_event,
        # ViewerState.scene_event); a full check runs at most every
        # ROP_RECHECK_SECONDS instead of on every 16 ms poll.
        node = self.__dict__.get("_rop_node")
        if node is None:
            return None
        now = time.monotonic()
        if now < self.__dict__.get("_rop_valid_until", 0.0):
            return node
        try:
            identity = self.__dict__.get("_rop_identity")
            alive = node.sessionId() == identity if identity is not None else node.sessionId() is not None
            if alive and identity is not None:
                alive = hou.nodeBySessionId(identity) is not None
        except hou.Error:
            alive = False
        if not alive:
            self._rop = None
            return None
        self._rop_valid_until = now + ROP_RECHECK_SECONDS
        return node

    @_rop.setter
    def _rop(self, node):
        watched = self.__dict__.get("_rop_watched")
        if watched is not None and watched is not node:
            self._rop_watched = None
            try:
                watched.removeEventCallback(_rop_events(), self._rop_event)
            except (hou.Error, AttributeError):
                pass  # Already deleted: its callbacks went with it.
        self._rop_node = node
        self._rop_valid_until = 0.0
        if node is not None and node is not watched:
            try:
                node.addEventCallback(_rop_events(), self._rop_event)
                self._rop_watched = node
            except (hou.Error, AttributeError):
                self._rop_watched = None

    def _rop_event(self, event_type, **_kwargs):
        """Houdini event of the selected ROP: forget a deleted node at once, follow a rename."""
        node = self.__dict__.get("_rop_node")
        if node is None:
            return
        if event_type == hou.nodeEventType.BeingDeleted:
            self._rop_watched = None  # Houdini drops the node's callbacks with it.
            self._rop_node = None
            self._rop_valid_until = 0.0
            if not self._disposed:
                QtCore.QTimer.singleShot(0, self._rop_deleted)
        elif event_type == hou.nodeEventType.NameChanged and not self._disposed:
            try:
                self.rop_path.refresh()  # Follows the node by identity.
            except hou.Error:
                pass

    def _rop_deleted(self):
        if not self._disposed:
            self.update_render_controls()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rop = None
        self._rop_identity = None
        self.entry = None
        self._observer = None
        self._disposed = False
        self._pending = False
        self._update_clock = UpdateClock()
        viewers.add(self)
        self._sequence = 0
        self._settings_dirty = True
        self._session = None
        self._frame_session = None
        self._presenting = False
        self._force_render_pending = False
        from .gpu_display import Converter

        self._display_queue = DisplayQueue(Converter())
        self._display_epoch = 0
        self._frame_wakeup = ViewerWakeup(self.present_available, self._display_queue.is_current)
        self._display_queue.on_ready = self._frame_wakeup.notify
        self._display_queue.on_first_ready = self._frame_wakeup.notify_first
        self._render_queued = False
        self._in_render = False
        self._update_serial = 0
        self._pose_preview_queued = False
        self._camera_update_queued = False
        self._stopped = False
        self._stop_requested = False
        self._render_mode = False
        self._comparison_available = False
        self._locked_camera = None
        self._unlinked_viewports = []
        self._focus_capture = None
        # Pick tools: a Ctrl+click waiting for the picking planes, the pixel readout's
        # cached frame, and the pass shown (with its channel names).
        self._pending_pick = None
        self.last_pick_route = None
        self.last_readout = None
        self._inspection = None
        self._displayed_pass = "combined"
        self._displayed_channels = ()
        self._changed_at = 0.0
        self._pending_since = 0.0
        self._last_time = hou.time()
        self._timeline_pending_time = None
        self._snapshots = []
        self._live_image = None
        self._saved_output_directories = {}
        self._pending_save = None
        self._final_view = None
        self._final_queued = False
        self._deferred_render = False
        self._notice = None
        self._status_key = None
        self._status_due = 0.0
        self._status_applied = None
        self._live_frame_metadata = {}
        self._live_generation = 0
        self._previous_color_notice = ""
        self._fullscreen = None
        self._pass_color = True
        self._pass_alpha = True
        self._last_memory_poll = 0
        self.setWindowTitle("Cycles Render")
        layout = QtWidgets.QVBoxLayout(self)
        header = QtWidgets.QHBoxLayout()
        layout.addLayout(header)
        self.final_render_button = ViewerToolButton()
        self.final_render_button.clicked.connect(self.queue_final_render)
        self.final_render_button.setToolTip(
            "Render the selected ROP frame range using its final settings and Output Picture. Show the current frame here. Stop cancels the sequence; Play returns to IPR."
        )
        self.render_button = ViewerToolButton()
        self.render_button.clicked.connect(self.queue_render)
        self.pause_button = ViewerToolButton()
        self.pause_button.setCheckable(True)
        self.pause_button.clicked.connect(self.pause)
        self.stop_button = ViewerToolButton()
        self.stop_button.clicked.connect(self.cancel)
        controls = QtWidgets.QVBoxLayout()
        header.addLayout(controls)
        self.refresh_button = ViewerToolButton()
        self.refresh_button.setToolTip(
            "Apply the latest scene changes without stopping the active render mode. Useful when Auto is off."
        )
        self.refresh_button.clicked.connect(self.queue_render)
        self.render_controls = RenderControls(
            (self.render_button, self.pause_button, self.stop_button, self.refresh_button)
        )
        controls.addWidget(self.render_controls)
        auto_row = QtWidgets.QHBoxLayout()
        controls.addLayout(auto_row)
        self.preview_interactive = QtWidgets.QCheckBox("Interactive")
        auto_row.addWidget(self.preview_interactive)
        self.preview_interactive.setToolTip(
            "On: use the ROP Interactive Render settings. Off: use Final Render settings. Auto controls whether scene edits update the image."
        )
        self.preview_interactive.toggled.connect(self.change_preview_interactive)
        self.tessellation_update = ViewerToolButton()
        self.tessellation_update.setCheckable(True)
        self.tessellation_update.setChecked(True)
        self.tessellation_update.setEnabled(False)
        self.tessellation_update.setToolTip(
            "Adaptive Subdivision Update recalculates the adaptive mesh automatically. Off retains existing tessellation. An uncached mesh renders its original polygons with their source normals. Disk renders use current settings."
        )
        self.tessellation_update.toggled.connect(self.change_tessellation_update)
        self.tessellation_refresh = ViewerToolButton()
        self.tessellation_refresh.setEnabled(False)
        self.tessellation_refresh.setToolTip(
            "Refresh Tessellation recalculates once for the current scene and view, including when Adaptive Subdivision Update is off."
        )
        self.tessellation_refresh.clicked.connect(self.refresh_tessellation)
        self.auto = ViewerToolButton()
        self.auto.setText("Auto")
        self.auto.setCheckable(True)
        self.auto.setChecked(True)
        auto_row.addWidget(self.auto)
        self.auto.setToolTip(
            "Automatically update the running render after scene edits. Stop holds updates until Render is pressed again."
        )
        quality = QtWidgets.QHBoxLayout()
        controls.addLayout(quality)
        self.preview_denoise = ViewerToolButton()
        self.preview_denoise.setText("Denoise")
        self.preview_denoise.setCheckable(True)
        auto_row.addWidget(self.preview_denoise)
        auto_row.addStretch()
        self.preview_denoise.setToolTip(
            "Denoise: switch denoising in the selected ROP settings (Interactive Render in Interactive mode, Final Render in Final)."
        )
        self.preview_denoise.toggled.connect(self.change_preview_denoise)
        quality.addWidget(QtWidgets.QLabel("Resolution"))
        self.preview_resolution = _combo_box()
        quality.addWidget(self.preview_resolution)
        self.preview_resolution.setEditable(True)
        self.preview_resolution.addItem("ROP %", 0)
        for percent in (10, 25, 50, 75, 100, 150, 200):
            self.preview_resolution.addItem(str(percent) + "%", percent)
        self.preview_resolution.setToolTip(
            "Viewer render resolution as a percentage of ROP Width/Height, in either sampling mode. Disk and clapper renders use Common Settings Resolution %. ROP % explicitly follows that percentage. Display zoom is separate."
        )
        self.preview_resolution.activated.connect(self.change_preview_resolution)
        self.preview_resolution.lineEdit().editingFinished.connect(self.change_preview_resolution)

        selectors = QtWidgets.QGridLayout()
        header.addLayout(selectors, 1)
        selectors.addWidget(QtWidgets.QLabel("ROP"), 0, 0)
        from .viewer_nodes import NodeMenu

        self.rop_path = NodeMenu(hou.ropNodeTypeCategory(), "cycles_ipr", "Choose Cycles ROP")
        selectors.addWidget(self.rop_path, 0, 1)
        self.rop_path.nodeSelected.connect(self.set_rop)
        self.rop_jump = ViewerToolButton()
        selectors.addWidget(self.rop_jump, 0, 2)
        self.rop_jump.clicked.connect(lambda: self.jump_to_node(self.rop_path.currentData()))
        self.camera = NodeMenu(hou.objNodeTypeCategory(), "cam", "Choose Camera")
        selectors.addWidget(self.camera, 1, 1)
        self.camera.nodeSelected.connect(self.choose_camera)
        self.camera_jump = ViewerToolButton()
        selectors.addWidget(self.camera_jump, 1, 2)
        self.camera_jump.clicked.connect(lambda: self.jump_to_node(self.camera.currentData()))
        self.camera_lock = ViewerToolButton()
        self.camera_lock.setText("Lock Camera")
        self.camera_lock.setCheckable(True)
        self.camera_lock.setToolTip(
            "Keep this render camera and disconnect viewport navigation from it. Explicit camera parameter edits still update rendering."
        )
        selectors.addWidget(self.camera_lock, 1, 3)
        self.camera_lock.toggled.connect(self.lock_camera)
        selectors.setColumnStretch(1, 1)
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        self.pass_menu = _combo_box()
        self.pass_menu.addItem("Beauty", "combined")
        row.addWidget(self.pass_menu)
        self.pass_menu.currentIndexChanged.connect(self.display_changed)
        self.channels = _combo_box()
        for label, token in (("RGBA", "rgba"), ("RGB", "rgb"), ("R", "r"), ("G", "g"), ("B", "b"), ("Alpha", "alpha")):
            self.channels.addItem(label, token)
        self.channels.setToolTip(
            "Inspect color passes or their alpha, where available. RGB exposes emission with zero alpha. Alpha bypasses color transforms, exposure and gamma. Scalar/vector data uses its raw display."
        )
        row.addWidget(self.channels)
        self.channels.currentIndexChanged.connect(self.display_changed)
        self.channel_alpha = ViewerToolButton()
        self.channel_alpha.setText("×A")
        self.channel_alpha.setCheckable(True)
        self.channel_alpha.setChecked(True)
        self.channel_alpha.setProperty("cycles_view_toggle", True)
        self.channel_alpha.setAccessibleName("Apply coverage alpha to color channels")
        self.channel_alpha.setToolTip(
            "Apply coverage alpha when viewing RGB, R, G or B. Off shows the image over black (premultiplied color, as Nuke's RGB view), opaque. RGBA always uses alpha; A shows alpha itself. Display only; rendered files are unchanged."
        )
        self.channel_alpha.toggled.connect(self.display_changed)
        row.addWidget(self.channel_alpha)
        self.checker = ViewerToolButton()
        self.checker.setCheckable(True)
        self.checker.setChecked(True)
        self.checker.setProperty("cycles_view_toggle", True)
        self.checker.setAttribute(QtCore.Qt.WA_Hover, True)
        self.checker.setToolTip("Toggle transparency checkerboard. Click again for the default background.")
        self.checker.setAccessibleName("Checkerboard")
        icon = QtGui.QPixmap(16, 16)
        icon.fill(QtGui.QColor(92, 92, 92))
        painter = QtGui.QPainter(icon)
        painter.fillRect(0, 0, 8, 8, QtGui.QColor(184, 184, 184))
        painter.fillRect(8, 8, 8, 8, QtGui.QColor(184, 184, 184))
        painter.end()
        checker_icon = QtGui.QIcon()
        # Keep the glyph grayscale in every enabled state. The button's
        # background indicates hover/selection using the native theme colors.
        for mode in (QtGui.QIcon.Normal, QtGui.QIcon.Active, QtGui.QIcon.Selected):
            for state in (QtGui.QIcon.Off, QtGui.QIcon.On):
                checker_icon.addPixmap(icon, mode, state)
        self.checker.setIcon(checker_icon)
        row.addWidget(self.checker)
        self.checker.toggled.connect(self.set_checker)
        from .viewer_background import BackgroundColorButton

        self.background_button = BackgroundColorButton(self)
        row.addWidget(self.background_button)
        self.show_noisy = ViewerToolButton()
        self.show_noisy.setText("Show Noisy")
        self.show_noisy.setCheckable(True)
        self.show_noisy.setProperty("cycles_view_toggle", True)
        self.show_noisy.setToolTip(
            "Compare Beauty before denoising. Display only: does not change denoising settings or restart rendering. Requires denoised Beauty and its noisy pass; unavailable for snapshots and other AOVs."
        )
        self.show_noisy.setEnabled(False)
        self.show_noisy.toggled.connect(self.display_changed)
        row.addWidget(self.show_noisy)
        self.focus_pick = ViewerToolButton()
        self.focus_pick.setText("Pick Focus")
        self.focus_pick.setCheckable(True)
        self.focus_pick.setToolTip(
            "Click a rendered surface to set camera focus and enable camera depth of field. Toggling this tool does not restart rendering. Space/middle-drag still pans."
        )
        row.addWidget(self.focus_pick)
        self.focus_pick.toggled.connect(self.toggle_focus_pick)
        self.clay = ViewerToolButton()
        self.clay.setText("Clay")
        self.clay.setCheckable(True)
        self.clay.setProperty("cycles_view_toggle", True)
        self.clay.setAccessibleName("Clay render")
        self.clay.setToolTip(
            "Clay: render every surface in neutral grey to judge light and form; scene materials and the ROP are not changed."
        )
        self.clay.toggled.connect(self.toggle_clay)
        row.addWidget(self.clay)
        policy = color_policy()
        self.display = _combo_box()
        self.view = _combo_box()
        self._color_configuration_id = policy["configuration_id"]
        self._last_color_poll = 0.0
        self.display.addItems(list(policy["views"]))
        self.display.setCurrentText(policy["default_display"])
        row.addWidget(self.display)
        row.addWidget(self.view)
        self.display.currentTextChanged.connect(self.populate_views)
        self.view.currentTextChanged.connect(self.display_changed)
        self.populate_views(self.display.currentText())
        navigation = QtWidgets.QGridLayout()
        layout.addLayout(navigation)
        row = QtWidgets.QHBoxLayout()
        navigation.addLayout(row, 0, 0, QtCore.Qt.AlignLeft)
        self.region = ViewerToolButton()
        self.region.setText("Region")
        self.region.setCheckable(True)
        row.addWidget(self.region)
        self.region.setToolTip(
            "Region: drag a render region on the image (Shift+drag works without it); uncheck to render the whole image."
        )
        self.region.toggled.connect(self.toggle_region)
        display_controls = QtWidgets.QHBoxLayout()
        navigation.addLayout(display_controls, 0, 1, QtCore.Qt.AlignHCenter)
        self.exposure = DisplayFloat(-16, 16, 0)
        self.exposure_label = DragLabel("Exposure", self.exposure, 0.025)
        display_controls.addWidget(self.exposure_label)
        display_controls.addWidget(self.exposure)
        self.exposure.valueChanged.connect(self.display_changed)
        self.gamma = DisplayFloat(0.05, 8, 1)
        self.gamma_label = DragLabel("Gamma", self.gamma, 0.01)
        display_controls.addWidget(self.gamma_label)
        display_controls.addWidget(self.gamma)
        self.gamma.valueChanged.connect(self.display_changed)
        self.exposure.setToolTip(
            "Display exposure in stops. Middle-mouse drag uses the Houdini value ladder. Ctrl+middle-click resets to default. Does not restart rendering or change the EXR."
        )
        self.gamma.setToolTip(
            "Display gamma, applied after the view transform. Middle-mouse drag uses the Houdini value ladder. Ctrl+middle-click resets to default. Raw data and alpha are unchanged."
        )
        row = QtWidgets.QHBoxLayout()
        navigation.addLayout(row, 0, 2, QtCore.Qt.AlignRight)
        navigation.setColumnStretch(0, 1)
        navigation.setColumnStretch(2, 1)
        self.fit_button = ViewerToolButton()
        self.fit_button.setText("Fit")
        self.fit_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.fit_button.setCheckable(True)
        row.addWidget(self.fit_button)
        self.fit_button.setProperty("cycles_view_toggle", True)
        self.fit_button.setAttribute(QtCore.Qt.WA_Hover, True)
        self.fit_button.setToolTip(
            "Center the whole image and keep it fitted when the viewer is resized. Home or Shift+F."
        )
        self.zoom_menu = _combo_box()
        self.zoom_menu.setMinimumContentsLength(7)
        self._zoom_presets = (12.5, 25.0, 50.0, 75.0, 100.0, 150.0, 200.0, 400.0, 800.0)
        for percent in self._zoom_presets:
            self.zoom_menu.addItem(f"{percent:g}%", percent)
        self.zoom_menu.setToolTip(
            "Image zoom. 100% maps one image pixel to one physical display pixel. Also shows the current wheel or fitted zoom."
        )
        row.addWidget(self.zoom_menu)
        self.zoom_menu.currentIndexChanged.connect(self.change_zoom)
        self.fullscreen_button = ViewerToolButton()
        self.fullscreen_button.setAccessibleName("Fullscreen image")
        self.fullscreen_button.setToolTip(
            "Fullscreen image. Move to the top or bottom edge for controls; they hide shortly after the pointer leaves. Escape returns to the panel."
        )
        self.fullscreen_button.setIcon(
            hou.qt.Icon("BUTTONS_maximize")
            if hou.isUIAvailable()
            else self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarMaxButton)
        )
        row.addWidget(self.fullscreen_button)
        self.fullscreen_button.clicked.connect(self.open_fullscreen)
        from . import viewer_display

        # Read once per panel: classic, native (own window) or gpu (own window, OpenGL).
        self.canvas = viewer_display.create_canvas()
        if self.canvas.display_mode != "classic" and parent is None:
            # As Houdini's RE_Window: the viewer root is the one native window
            # in the pane; the canvas inside it stays an ordinary child. A viewer
            # built inside a Qt window (the shelf IPR window) is not native: a
            # native root there never shows the OpenGL canvas (blank window).
            viewer_display.make_native(self)
        layout.addWidget(self.canvas, 1)
        self.canvas.regionSelected.connect(self.set_region)
        self.canvas.framePainted.connect(self.frame_presented)
        self.canvas.fullscreenExitRequested.connect(self.close_fullscreen)
        self.canvas.regionCleared.connect(self.reset_region)
        self.fit_button.clicked.connect(self.canvas.fit_image)
        self.canvas.viewChanged.connect(self.update_view_controls)
        self.update_view_controls()
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        self.snapshot_button = ViewerToolButton()
        row.addWidget(self.snapshot_button)
        self.snapshot_button.setToolTip(
            "Keep the displayed image in panel memory. Snapshots are taken only when you click this button."
        )
        self.snapshot_button.clicked.connect(self.snapshot)
        self.snapshot_menu = _combo_box()
        self.snapshot_menu.addItem("Live")
        row.addWidget(self.snapshot_menu)
        self.snapshot_menu.currentIndexChanged.connect(self.show_snapshot)
        from .viewer_chrome import HistorySlider

        self.history_slider = HistorySlider()
        self.history_slider.setRange(1, 1)
        self.history_slider.setEnabled(False)
        self.history_slider.setSingleStep(1)
        self.history_slider.setPageStep(1)
        self.history_slider.setAccessibleName("Render history")
        row.addWidget(self.history_slider, 1)
        self.history_slider.setToolTip(
            "Snapshots, oldest to newest. The rightmost position is Live. Saved display images retain their view transform; passes and linear EXR are available in Live. Up to 8 images within 64 MiB. Rendering does not save snapshots automatically."
        )
        self.history_slider.valueChanged.connect(self.select_history)
        self.history_number = QtWidgets.QLabel("Live")
        row.addWidget(self.history_number)
        self.save_button = ViewerToolButton()
        row.addWidget(self.save_button)
        self.save_button.clicked.connect(self.save_exr)
        self.save_format = _combo_box()
        for label, token in (("EXR", "exr"), ("PNG", "png"), ("JPEG", "jpg")):
            self.save_format.addItem(label, token)
        self.save_format.setToolTip(
            "EXR saves completed linear render passes using ROP output settings. PNG and JPEG save the displayed image, even while rendering; PNG retains alpha and JPEG uses a black background."
        )
        self.save_format.currentIndexChanged.connect(lambda: self.update_save_control())
        self.info = StatusLine("Ready")
        layout.addWidget(self.info)
        self.memory_details = QtWidgets.QCheckBox("Memory")
        layout.addWidget(self.memory_details)
        self.memory_details.setChecked(True)
        self.memory_info = StatusLine()
        layout.addWidget(self.memory_info)
        self.memory_info.setVisible(False)
        self.memory_details.setToolTip(
            "Show compact RAM and active render GPU usage in the status row. Hover for process and card details. Sampled once per second."
        )
        self.memory_details.toggled.connect(lambda enabled: self.poll_memory(force=True))
        self.texture_details = QtWidgets.QCheckBox("Texture color spaces")
        layout.addWidget(self.texture_details)
        self.texture_info = QtWidgets.QPlainTextEdit()
        self.texture_info.setReadOnly(True)
        self.texture_info.setMaximumHeight(120)
        self.texture_info.setVisible(False)
        layout.addWidget(self.texture_info)
        self.texture_details.setToolTip(
            "Input color spaces used by the current render. Pending edits keep the previous render inputs until an update succeeds."
        )
        self.texture_details.toggled.connect(lambda enabled: self.poll_texture_inputs(force=True))
        self._texture_inputs_revision = None
        from . import viewer_layout

        viewer_layout.apply(self)
        from .viewer_resolution import ResolutionLink

        self._resolution_linker = ResolutionLink(self)
        from .viewer_shortcuts import ViewerShortcuts

        self._shortcuts = ViewerShortcuts(self)
        self.update_render_controls()
        self.canvas.focusPicked.connect(self.pick_focus)
        self.canvas.focusCancelled.connect(lambda: self.focus_pick.setChecked(False))
        self.canvas.nodePicked.connect(self.pick_node)
        self.canvas.pixelInspected.connect(self.inspect_pixel)
        self.canvas.inspectionEnded.connect(self.end_inspection)
        self.monitor_scale = MonitorScale(self)
        self._next_scale_poll = 0.0
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(16)
        # Session log stall monitor: a beat before and a check after each poll (Qt calls the slots in this
        # order); poll itself stays the slot that tests and benchmarks replace.
        self.timer.timeout.connect(self._stall_beat)
        self.timer.timeout.connect(self.poll)
        self.timer.timeout.connect(self._stall_after)
        self.timer.start()
        # Visible: poll every 16 ms. Hidden but open: no polling, painting or
        # display conversion. Closed: dispose, even when Houdini never calls
        # onDestroyInterface. See viewer_lifetime for what Houdini reports.
        self._lifetime = viewer_lifetime.Lifetime()
        self._host_tab = None
        self._revive_posted = False
        self._hidden_paused = None
        self._liveness_timer = QtCore.QTimer(self)
        self._liveness_timer.setSingleShot(True)
        self._liveness_timer.timeout.connect(self.check_host)
        self._resume_timer = QtCore.QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.setInterval(0)
        self._resume_timer.timeout.connect(self._resume_poll)
        self.canvas.installEventFilter(self)
        from .viewer_fullscreen import ExposeRepaint

        self._expose_repaint = ExposeRepaint(self.canvas)
        self._expose_watched = None
        from .viewer_state import ViewerState

        self._viewer_state = ViewerState(self)

    def update_render_controls(self, status=None):
        status = status or (self.entry.session.status() if self.entry else {"state": "idle"})
        state = status["state"]
        # A pending scene can be reported as "rendering" while the retained
        # engine is paused. The toggle must describe the real pause state, so
        # the next click resumes instead of accidentally asking to pause again.
        backend_state = status.get("backend_state", state)
        busy = self._render_queued or self._in_render or self._stop_requested
        if state in ("failed", "closed") or (
            self.entry
            and self.entry.channel
            and state == "cancelled"
            and not status.get("update_rejected")
            and not self._in_render
            and not self._render_queued
        ):
            self._render_mode = False
        final_running = bool(self._final_view and self._final_view.running) or self._final_queued
        active = (self._render_mode and not self._stopped) or final_running or self._deferred_render
        paused = bool(self.entry and self.entry.paused)
        key = (
            self._rop is not None,
            busy,
            final_running,
            active,
            paused,
            self._stop_requested,
            self._final_view is None,
            self.resolution_link.isChecked(),
            self._deferred_render,
        )
        if key == getattr(self, "_render_controls_key", None):
            return
        self._render_controls_key = key
        self.output_folder_button.setEnabled(self._rop is not None)
        self.final_render_button.setEnabled(self._rop is not None and not busy and not final_running)
        self.render_button.setEnabled(self._rop is not None and not busy and not active)
        self.pause_button.setEnabled(active and not self._stop_requested and not self._deferred_render)
        self.stop_button.setEnabled(active and not self._stop_requested)
        self.refresh_button.setEnabled(active and not paused and not busy and self._final_view is None)
        for control in (
            self.rop_path,
            self.camera,
            self.camera_jump,
            self.rop_jump,
            self.mode_menu,
            self.preview_denoise,
            self.preview_resolution,
            self.resolution_link,
            self.auto,
            self.camera_lock,
            self.region,
        ):
            control.setEnabled(
                not final_running and (self._final_view is None or control in (self.rop_path, self.rop_jump))
            )
        self.preview_resolution.setEnabled(self.preview_resolution.isEnabled() and not self.resolution_link.isChecked())
        self.pause_button.blockSignals(True)
        self.pause_button.setChecked(paused and active)
        self.pause_button.blockSignals(False)
        self.pause_button.setToolTip("Resume rendering" if paused and active else "Pause rendering")
        self.pause_button.setAccessibleName(self.pause_button.toolTip())

    def update_view_controls(self):
        fit = self.canvas.view_mode == "fit"
        if self.fit_button.isChecked() != fit:
            self.fit_button.setChecked(fit)
        if hasattr(self, "actual_pixels"):
            self.actual_pixels.setChecked(self.canvas.view_mode == "actual")
        percent = self.canvas.zoom_percent()
        self.zoom_menu.blockSignals(True)
        try:
            # Keep preset positions stable while showing arbitrary fitted/wheel
            # scales in this same control, without another zoom indicator.
            index = next(
                (i for i, preset in enumerate(self._zoom_presets) if math.isclose(percent, preset, rel_tol=1e-6)), -1
            )
            if index < 0:
                label = f"{percent:.1f}%"
                index = len(self._zoom_presets)
                if self.zoom_menu.count() == index:
                    self.zoom_menu.addItem(label, None)
                elif self.zoom_menu.itemText(index) != label:
                    self.zoom_menu.setItemText(index, label)
            elif self.zoom_menu.count() > len(self._zoom_presets):
                self.zoom_menu.removeItem(len(self._zoom_presets))
            if self.zoom_menu.currentIndex() != index:
                self.zoom_menu.setCurrentIndex(index)
        finally:
            self.zoom_menu.blockSignals(False)

    def change_zoom(self, *_):
        percent = self.zoom_menu.currentData()
        if percent is not None:
            self.canvas.set_zoom(percent)

    def set_checker(self, enabled):
        self.canvas.checker_enabled = enabled
        self.canvas.update()
        self.backgrounds_changed()

    def sync_preview_controls(self):
        if self._disposed or not self._rop:
            return
        interactive = native_scene.interactive_settings(self._rop)
        self.preview_interactive.blockSignals(True)
        self.preview_interactive.setChecked(interactive)
        self.preview_interactive.blockSignals(False)
        # The hidden compatibility control is updated with signals blocked.
        # Synchronize the visible menu explicitly when binding/editing a ROP.
        if hasattr(self, "mode_menu"):
            blocked = self.mode_menu.blockSignals(True)
            self.mode_menu.setCurrentIndex(0 if interactive else 1)
            self.mode_menu.blockSignals(blocked)
        enabled = native_scene.denoising_configuration(self._rop, hou.time())["enabled"]
        self.preview_denoise.blockSignals(True)
        self.preview_denoise.setChecked(enabled)
        self.preview_denoise.blockSignals(False)
        update = self._rop.parm("tessellation_update")
        self.tessellation_update.setEnabled(update is not None)
        self.tessellation_refresh.setEnabled(update is not None)
        blocked = self.tessellation_update.blockSignals(True)
        self.tessellation_update.setChecked(bool(update is None or update.eval()))
        self.tessellation_update.blockSignals(blocked)
        if hasattr(self, "_resolution_linker"):
            self._resolution_linker.sync_reference()
        percent = self._rop.evalParm("ipr_resolution_percent")
        self.preview_resolution.blockSignals(True)
        self.preview_resolution.setCurrentText(f"{percent:.2f}".rstrip("0").rstrip(".") + "%" if percent else "ROP %")
        self.preview_resolution.blockSignals(False)

    def refresh_tessellation(self):
        if self._rop is None:
            return
        from ..scene import tessellation

        tessellation.refresh(self._rop)

    def change_tessellation_update(self, enabled):
        rop = self._rop
        if rop is None:
            return
        parm = rop.parm("tessellation_update")
        if parm is None:
            return
        parm.set(bool(enabled))
        self.request_update()
        # An explicit policy change applies even with Auto off, without resuming
        # a stopped or paused session.
        if (
            self._render_mode
            and self.entry
            and not self.entry.paused
            and not self.entry.final_paused
            and not self._stopped
        ):
            self.queue_render()

    def change_preview_denoise(self, enabled):
        if self._rop:
            native_scene.set_interactive_denoising(self._rop, enabled)
            self.request_update()

    def change_preview_interactive(self, enabled):
        rop = self._rop
        if rop is None:
            return
        parm = rop.parm("viewer_interactive")
        if bool(parm.eval()) == bool(enabled):
            self.sync_preview_controls()
            return
        parm.set(bool(enabled))
        self.sync_preview_controls()
        self.request_update()
        # Interactive/Final is an explicit render-settings choice: start a fresh
        # render with the selected settings, also when both modes resolve to
        # identical values or Auto update is off. Stop and Pause keep their meaning.
        if (
            self._render_mode
            and self.entry
            and not self.entry.paused
            and not self.entry.final_paused
            and not self._stopped
        ):
            self.queue_render()

    def change_preview_resolution(self, *args):
        if not self._rop:
            return
        try:
            text = self.preview_resolution.currentText().strip()
            percent = 0 if text == "ROP %" else float(text.rstrip("%"))
            if not math.isfinite(percent) or (text != "ROP %" and not 0 < percent <= 400):
                raise ValueError("Use a positive percentage up to 400% or ROP %")
            rop = self._rop
            if rop is None:
                return
            parm = rop.parm("ipr_resolution_percent")
            # editingFinished also fires when focus merely leaves the field.
            # An unchanged value must not restart rendering.
            if parm.eval() == percent:
                return
            parm.set(percent)
            self.request_update()
        except (ValueError, hou.Error) as error:
            self.notify(str(error))
            self.sync_preview_controls()

    def jump_to_node(self, path):
        node = hou.node(path) if path else None
        if not node or not hou.isUIAvailable():
            return
        pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if pane is not None:
            pane.setCurrentNode(node)

    def choose_camera(self, node):
        self.camera.set_path(node.path())
        self.change_camera()

    def lock_camera(self, enabled):
        self.camera.setEnabled(not enabled)
        if enabled and self._rop:
            self._locked_camera = self._rop.evalParm("camera")
            if hou.isUIAvailable():
                for pane in hou.ui.paneTabs():
                    if pane.type() != hou.paneTabType.SceneViewer:
                        continue
                    for view in pane.viewports():
                        camera = view.camera()
                        if camera and camera.path() == self._locked_camera and view.isCameraLockedToView():
                            self._unlinked_viewports.append((view, camera))
                            view.lockCameraToView(False)
        else:
            for view, camera in self._unlinked_viewports:
                try:
                    if view.camera() == camera:
                        view.lockCameraToView(True)
                except hou.Error:
                    pass
            self._unlinked_viewports = []
            self._locked_camera = None

    def toggle_focus_pick(self, enabled):
        self.canvas.set_focus_enabled(enabled)
        if self.entry:
            self.entry.session.set_focus_pick(enabled or self._pick_armed())

    def _focus_transport(self):
        """Picking planes are transported while Pick Focus is armed or a Ctrl+click waits for them."""
        return self.focus_pick.isChecked() or self._pick_armed()

    def _pick_armed(self):
        return self._pending_pick is not None and self._pending_pick[4]

    def _pick_sample(self, x, y):
        """The renderer's camera hit at (x, y) for the displayed live image, or None if not available yet."""
        if getattr(self.canvas, "frame_metadata", {}).get("display_epoch") != self._display_epoch:
            return None
        frame = self.entry.session.focus_sample(x, y)
        if (
            not frame
            or frame["generation"] != self.entry.session.status()["generation"]
            or frame["generation"] != self._live_generation
        ):
            return None
        displayed = getattr(self.canvas, "frame_metadata", {})
        if frame.get("render_revision", frame["generation"]) != displayed.get("render_revision"):
            return None
        return frame

    def pick_node(self, x, y, kind):
        """Ctrl+click: select the object under the pointer; Ctrl+double-click: its material."""
        try:
            if not self.entry or self.snapshot_menu.currentIndex() != 0 or self._final_view is not None:
                raise ValueError("Select objects on the live render")
            armed = self._pick_armed()
            # Picking planes are sent once per rendered frame. Request them only while frames
            # still arrive: a finished render would not send them again, and asking would
            # use up the one a later Pick Focus needs. Otherwise trace a host ray.
            arm = not armed and not self.focus_pick.isChecked() and self.entry.session.status()["state"] == "rendering"
            self._pending_pick = (x, y, kind, time.monotonic() + 1.0, armed or arm)
            if arm:
                self.entry.session.set_focus_pick(True)
            self._resolve_pending_pick()
        except (ValueError, hou.Error) as error:
            self.notify(str(error))

    def _finish_pending_pick(self):
        pending, self._pending_pick = self._pending_pick, None
        if pending is not None and pending[4] and self.entry and not self.focus_pick.isChecked():
            self.entry.session.set_focus_pick(False)

    def _resolve_pending_pick(self):
        """Use the renderer's own camera hit when it is (or soon becomes) available, else a host ray."""
        pending = self._pending_pick
        if pending is None:
            return
        x, y, kind, deadline, armed = pending
        try:
            if not self.entry or self._rop is None:
                raise ValueError("Select objects on the live render")
            camera = hou.node(self._rop.evalParm("camera"))
            if camera is None:
                raise ValueError("Choose a render camera")
            from . import viewer_pick

            sample = self._pick_sample(x, y)
            if sample is None and armed and time.monotonic() < deadline:
                return  # Picking planes arrive with the next progressive frame.
            self._finish_pending_pick()
            if sample is not None:
                depth = sample.get("depth", math.inf)
                if not math.isfinite(depth) or depth <= 0 or depth >= 1e10:
                    self.notify("No object at this pixel")
                    return
                hit = viewer_pick.identify(self._rop, camera, position=sample["position"])
                self.last_pick_route = "renderer"
                if hit is None:
                    raise ValueError("Could not identify the object at this pixel")
            else:
                # A finished render sends no new picking planes: trace the camera ray in Houdini.
                width, height = self.canvas.full_size
                ray = viewer_pick.camera_ray(camera, x, y, width, height, self.canvas.pixel_aspect())
                if ray is None:
                    raise ValueError("Enable Pick Focus to select through a panoramic camera")
                hit = viewer_pick.identify(self._rop, camera, ray=ray)
                self.last_pick_route = "ray"
                if hit is None:
                    self.notify("No object at this pixel")
                    return
            if kind == "material":
                node, source = viewer_pick.material(self._rop, hit)
                if node is None:
                    raise ValueError("The material of " + hit.owner.path() + " does not exist")
                viewer_pick.select(node)
                self.notify(f"Material {node.path()} ({source}) of {hit.owner.path()}")
            else:
                viewer_pick.select(hit.owner)
                self.notify("Selected " + hit.owner.path())
        except Exception as error:  # A pick never raises into the Qt event loop.
            self._finish_pending_pick()
            self.notify(str(error) or type(error).__name__)

    def _inspection_frame(self):
        """Linear planes of the live image, refreshed at most every 0.25 s while the readout is held."""
        if not self.entry or not self.displaying_live() or self._final_view is not None:
            return None
        now = time.monotonic()
        cached = self._inspection
        if cached is not None and (now < cached[0] or cached[1].get("sequence") == self._sequence):
            return cached[1]
        frame = self.entry.session.frame()
        if not frame or frame.get("generation") != self._live_generation:
            return None
        self._inspection = (now + 0.25, frame)
        return frame

    def inspect_pixel(self, x, y):
        """Hold the left button: status line shows the values under the pointer."""
        from . import viewer_pick

        display = viewer_pick.display_value(self.canvas.image, x, y, self.canvas.full_size)
        values = None
        live = self.snapshot_menu.currentIndex() == 0
        if live:
            try:
                frame = self._inspection_frame()
            except (ValueError, RuntimeError, hou.Error):
                frame = None
            if frame is not None:
                values = viewer_pick.plane_value(frame, self._displayed_pass, x, y)
        text = viewer_pick.readout_text(
            x,
            y,
            pass_label(self._displayed_pass) if live else self.snapshot_menu.currentText(),
            values,
            self._pass_color,
            display,
            self._displayed_channels,
        )
        self.last_readout = dict(x=x, y=y, values=values, display=display, text=text)
        self.notify(text, seconds=3600.0)

    def end_inspection(self):
        self._inspection = None
        self._notice = None
        self._status_key = None  # Restore the status headline at the next poll.

    def toggle_clay(self, enabled):
        """Clay is a viewer state applied to the viewer's own capture; nothing in the scene changes."""
        if self.entry:
            self.entry.changed()  # Materials change: never a camera-only update.
        self.request_update()
        if (
            self._render_mode
            and self.entry
            and not self.entry.paused
            and not self.entry.final_paused
            and not self._stopped
        ):
            self.queue_render()

    def pick_focus(self, x, y):
        from ..scene import camera_tools

        try:
            if not self.entry or self.snapshot_menu.currentIndex() != 0:
                raise ValueError("Pick focus on the live render")
            if getattr(self.canvas, "frame_metadata", {}).get("display_epoch") != self._display_epoch:
                raise ValueError("Wait for the current camera image before picking focus")
            frame = self.entry.session.focus_sample(x, y)
            camera = hou.node(self._rop.evalParm("camera"))
            if (
                not frame
                or frame["generation"] != self.entry.session.status()["generation"]
                or frame["generation"] != self._live_generation
            ):
                raise ValueError("Wait for the current camera image before picking focus")
            displayed = getattr(self.canvas, "frame_metadata", {})
            if frame.get("render_revision", frame["generation"]) != displayed.get("render_revision"):
                raise ValueError("Wait for the current camera image before picking focus")
            current = native_scene.capture(self._rop, camera_only=True)
            if not camera or self._focus_capture != native_scene.camera_signature(current, hou.time()):
                raise ValueError("Render the current camera before picking focus")
            distance = camera_tools.focus_distance(frame, x, y, camera)
            camera_tools.set_focus(camera, distance)
            self.notify(
                f"Focus distance: {distance:.4g} scene units"
                + (" (depth of field is disabled on the ROP)" if self._rop.evalParm("dof_mode") == "off" else "")
            )
            self.request_update()
            if self._render_mode and not self.entry.paused and not self.entry.final_paused:
                self.queue_render()
        except (ValueError, hou.Error) as error:
            self.notify(str(error))

    def set_rop(self, rop):
        if self._disposed:
            return
        try:
            # Bind the node even when its current device/settings are invalid.
            # Otherwise the viewer has no observer and cannot recover when the
            # artist fixes the ROP; Render remains silently disabled.
            if rop is None or rop.type().name() != "cycles_ipr":
                raise ValueError("Select a Cycles Render ROP")
            # Deleted HOM handles cannot even be compared during scene reload.
            # Keep the public node session identity independently of its handle.
            rop_identity = rop.sessionId()
            # Houdini can announce the current node repeatedly, including
            # immediately after a Python Panel starts its own render. Detaching
            # the only viewer here closes its session before we can reattach.
            if rop_identity == self._rop_identity and self._observer is not None:
                self.rop_path.set_path(rop.path())
                self.camera.set_path(rop.evalParm("camera"))
                self._attach_active()
                self.update_render_controls()
                return
            if self._observer:
                self._observer.close()
            if self.camera_lock.isChecked():
                self.camera_lock.setChecked(False)
            self._detach()
            self._rop = rop
            self._rop_identity = rop_identity
            self.rop_path.set_path(rop.path())
            self.camera.set_path(rop.evalParm("camera"))
            self.sync_preview_controls()
            self._observer = native_scene.SceneObserver(
                rop,
                self.request_update,
                lambda: self.entry.session.native_material_paths() if self.entry else (),
                camera_callback=self.request_camera_update,
                object_pose_callback=self.request_pose_update,
                stylesheet_resources=lambda: self.entry.session.native_stylesheet_resources() if self.entry else (),
                cop_sources=lambda: self.entry.session.native_cop_sources() if self.entry else None,
            )
            existing = self._attach_active()
            self.info.setText("Sharing the active native render" if existing else "Ready to render")
            if hasattr(self, "_viewer_state"):
                self._viewer_state.save(force=True)
            self._stopped = self._stop_requested = False
            self.update_render_controls()
        except Exception as error:
            self.info.setText(str(error))

    def _attach_active(self):
        if self._final_view is not None or self._final_queued:
            return None
        existing = registry.get(f"rop:{self._rop_identity}") if self._rop_identity is not None else None
        if existing is not None and existing is not self.entry:
            self._attach(existing)
        return existing

    def _attach(self, entry):
        if self.entry is entry and self._session is entry.session:
            return
        if self.entry is not entry:
            self._detach()
            self.entry = entry
            if self._final_view is None or entry is not self._final_view.entry:
                registry.attach(entry)
            self._render_mode = entry.session.status()["state"] in ("rendering", "paused", "complete")
        self._sequence = 0
        self._settings_dirty = True
        if self._session is not entry.session:
            self._update_clock.rebase()
        self._session = entry.session
        self._previous_color_notice = ""
        self.watch_frames(entry.session)
        self._display_epoch += 1
        self._display_queue.invalidate(None)
        self.follow_display(entry.session)
        entry.session.set_feedback(id(self), self.canvas.isVisible() and self.displaying_live())

    def _detach(self):
        self.watch_frames(None)
        self._render_mode = False
        self._comparison_available = False
        if self.entry:
            self.entry.session.set_feedback(id(self), False)
            if self._final_view is not None and self.entry is self._final_view.entry:
                self._final_view.close()
                self._final_view = None
            else:
                registry.detach(self.entry)
            self.entry = None
        if self._final_view is not None and not self._final_view.running:
            self._final_view.close()
            self._final_view = None

    def watch_frames(self, session):
        if not self.ui_active():
            session = None  # Hidden: no frame notifications; showing subscribes again.
        if self._frame_session is not session:
            self._display_queue.follow(None, self._display_epoch, {}, None)
            if self._frame_session is not None:
                self._frame_session.set_frame_wakeup(id(self), None)
            self._frame_session = session
            if session is not None:
                session.set_frame_wakeup(id(self), self._display_queue.source_ready)

    def display_options(self):
        selected = self.pass_menu.currentData() or "combined"
        if selected == "combined" and self._comparison_available and self.show_noisy.isChecked():
            selected = "noisy_combined"
        settings = dict(
            **{"pass": selected},
            exposure=self.exposure.value(),
            gamma=self.gamma.value(),
            channels=self.channels.currentData() if self._pass_color else "rgba",
            use_alpha=self.channel_alpha.isChecked(),
            backgrounds=self.canvas.display_backgrounds(),
        )
        self._requested_backgrounds = settings["backgrounds"]
        if self.display.currentText():
            settings.update(display=self.display.currentText(), view=self.view.currentText())
        return settings

    def reset_display_adjustments(self):
        self.exposure.setValue(0)
        self.gamma.setValue(1)

    def toggle_channel(self, token):
        if not self.channels.isEnabled() or (token == "alpha" and not self._pass_alpha):
            return
        selected = "rgba" if self.channels.currentData() == token else token
        self.channels.setCurrentIndex(self.channels.findData(selected))

    def displaying_live(self):
        return self.snapshot_menu.currentIndex() == 0 or (
            hasattr(self, "snapshot_compare") and self.snapshot_compare.isChecked()
        )

    def toggle_snapshot_compare(self):
        if self.snapshot_compare.isChecked() and not self._snapshots:
            self.snapshot_compare.setChecked(False)
            return
        if self.snapshot_compare.isChecked() and not self.snapshot_menu.currentIndex():
            self.snapshot_menu.setCurrentIndex(len(self._snapshots))
        self.show_snapshot(self.snapshot_menu.currentIndex())

    def follow_display(self, session):
        if not self.ui_active():
            return  # Hidden: convert nothing. present_available follows again when shown.
        from .. import _native

        options = dict(self.display_options(), compact_full_frame=True)
        self._display_queue.follow(session.display_state, self._display_epoch, options, _native.frame_info)

    def frame_presented(self, generation):
        metadata = getattr(self.canvas, "frame_metadata", {})
        if (
            not self._disposed
            and self.entry
            and self._session is self.entry.session
            and self.displaying_live()
            and metadata.get("display_epoch") == self._display_epoch
            and metadata.get("generation") == generation
        ):
            self.entry.session.notify_presented(generation, metadata.get("render_revision"))

    def request_camera_update(self):
        self.request_update(sync_controls=False, kind="camera")
        if (
            self._disposed
            or not self.ui_active()
            or self._camera_update_queued
            or not self.entry
            or not self._render_mode
            or not self.auto.isChecked()
            or self._stopped
            or self.entry.paused
            or self.entry.final_paused
        ):
            return
        self._camera_update_queued = True
        # Let both observers finish marking the edit, then use the existing
        # camera-only guard. No SOP acquisition runs in this Qt callback.
        QtCore.QTimer.singleShot(0, self._queue_camera_update)

    def _queue_camera_update(self):
        self._camera_update_queued = False
        if (
            self._disposed
            or not self.ui_active()
            or self._in_render
            or not self.entry
            or not self._render_mode
            or not self.auto.isChecked()
            or self._stopped
            or self.entry.paused
            or self.entry.final_paused
        ):
            return
        if self._camera_capture_allowed():
            self.queue_render(reuse_if_unchanged=True)

    def request_pose_update(self):
        self.request_update(sync_controls=False)
        self.queue_pose_preview()

    def queue_pose_preview(self):
        if (
            self._disposed
            or not self.ui_active()
            or self._pose_preview_queued
            or self._in_render
            or not self.entry
            or not self._render_mode
            or not self.auto.isChecked()
            or self._stopped
            or self.entry.paused
            or self.entry.final_paused
        ):
            return
        self._pose_preview_queued = True
        # OBJ evaluation can have channel dependencies. Keep acquisition on the
        # supported host callback, independently of the debounced SOP capture.
        hou.ui.postEventCallback(self._run_queued_pose_preview)
        # Houdini may hold idle callbacks back during a viewport drag. Without
        # animation, expressions or constraints on the moved objects, run the
        # preview from the Qt loop too, as camera navigation does (whichever
        # runs first takes it; the queued flag makes the other a no-op).
        QtCore.QTimer.singleShot(0, self._run_safe_pose_preview)

    def _run_safe_pose_preview(self):
        # Checked after both observers have marked the edit (pose sources known).
        if self._pose_preview_queued and self.entry and self.entry.pose_preview_host_safe():
            self._run_queued_pose_preview()

    def _run_queued_pose_preview(self):
        if not self._pose_preview_queued:
            return
        self._pose_preview_queued = False
        if (
            self._disposed
            or not self.ui_active()
            or self._in_render
            or not self.entry
            or not self._render_mode
            or not self.auto.isChecked()
            or self._stopped
            or self.entry.paused
            or self.entry.final_paused
        ):
            return
        self._in_render = True
        try:
            self.entry.preview_object_poses()
        except (ValueError, hou.Error):
            pass  # The pending authoritative capture reports source/configuration errors.
        except Exception as error:
            self.entry.fail(error)
            self.info.setText("Pose preview failed: " + str(error))
        finally:
            self._in_render = False

    def queue_final_render(self):
        if self._disposed or self._in_render or self._final_queued or self._rop is None:
            return
        self._final_queued = True
        self.update_render_controls()
        hou.ui.postEventCallback(self._run_final_render)

    def _run_final_render(self):
        if not self._final_queued:
            return
        self._final_queued = False
        if self._disposed or self._rop is None:
            return
        pause_token = previous_ipr = view = None
        retained = False
        try:
            from .viewer_final import FinalView

            # Validate all ROP/display values before acquiring renderer ownership.
            view = FinalView(self, self._rop)
            pause_token = registry.begin_local_final()
            previous_ipr = self._final_view.previous_ipr if self._final_view is not None else self.entry
            if previous_ipr is not None:
                registry.attach(previous_ipr)
                retained = True
            self._detach()
            view.pause_token, view.previous_ipr = pause_token, previous_ipr
            retained = False  # The new FinalView now owns the extra IPR reference.
            self._final_view = view
            self._deferred_render = False
            self._in_render = True
            self.snapshot_compare.setChecked(False)
            self.snapshot_menu.setCurrentIndex(0)
            self.update_render_controls()
            view.run()
        except Exception as error:
            if retained and previous_ipr is not None:
                registry.detach(previous_ipr)
            if self._final_view is view:
                self._final_view = None
            if view is not None:
                try:
                    view.close()
                except Exception:
                    pass
            if pause_token is not None:
                registry.abandon_local_final(pause_token)
            self._in_render = False
            if not self._disposed:
                self.notify("Final render stopped: " + str(error))
                self.update_render_controls()

    def queue_render(self, *_, reuse_if_unchanged=False):
        """Qt handlers only enqueue: cooks run after Houdini finishes its UI events.

        A Qt refresh timer can fire inside a viewport cook or a native menu's
        nested event loop. Starting another SOP cook there is not safe.
        """
        if self._disposed:
            return
        # Manual Render/Refresh takes precedence over a queued automatic poll.
        # Keep that intent through reentrant host notifications until dispatch.
        if not reuse_if_unchanged:
            self._force_render_pending = True
            if hasattr(self, "_update_clock"):
                self._start_update_clock("render", force=True)
        if self._render_queued:
            return
        if self._in_render:
            self.request_update()
            return
        if not hou.isUIAvailable():
            force = getattr(self, "_force_render_pending", False)
            self._force_render_pending = False
            self.render(reuse_if_unchanged=not force)
            return
        self._render_queued = True
        self.update_render_controls()
        hou.ui.postEventCallback(self._run_queued_render)
        # A pure camera capture neither traverses nor cooks SOPs. Do not make
        # navigation wait for Houdini's idle callback, which may be deferred
        # while a mouse drag is active. The guard is checked again at dispatch:
        # a concurrent geometry/material change must keep the host-safe route.
        if self._camera_capture_allowed():
            QtCore.QTimer.singleShot(0, self._run_queued_camera)

    def _camera_capture_allowed(self):
        if not self.entry or not self._rop or not self.entry.camera_only_pending:
            return False
        now = hou.time()
        try:
            motion = native_scene.motion_configuration(self._rop, now)
        except hou.Error:
            return False  # The host-safe render path supplies the parameter diagnostic.
        return self.entry.can_capture_camera_only(motion, now)

    def _run_queued_camera(self):
        entry = self.entry
        if (
            self._render_queued
            and not self._disposed
            and entry
            and self._camera_capture_allowed()
            and not entry.channel
            and not entry.error
        ):
            self._run_queued_render()

    def _run_queued_render(self):
        if not self._render_queued:
            return
        self._render_queued = False
        if not self._disposed:
            force = getattr(self, "_force_render_pending", False)
            if not force and (
                not self.auto.isChecked()
                or not self.ui_active()
                or self._stopped
                or (self.entry is not None and (self.entry.paused or self.entry.final_paused))
            ):
                # Controls may change after Auto posts this host callback, and
                # the viewer may have been hidden since (automatic updates
                # wait until it is shown). Keep source/timeline intent pending.
                return
            self._force_render_pending = False
            self.render(reuse_if_unchanged=not force)

    def render(self, *, reuse_if_unchanged=False):
        if self._disposed:
            return
        if registry.local_final_active():
            # A manual Play remains owned by this panel until the last local
            # final releases. Automatic refresh never replaces a retained final.
            if not reuse_if_unchanged:
                self._deferred_render = True
                self._stopped = False
            self.request_update()
            registry.ensure_polling()
            self.update_render_controls()
            return
        self._deferred_render = False
        if self._in_render:
            self.request_update()
            return
        if self._final_view is not None:
            self._detach()
            self._final_view = None
        # An update dispatched without a trigger seen here (shown again after
        # hidden edits, a registry change): its time starts no later than now.
        self._start_update_clock("scene", restart=False, force=True)
        host_started = time.monotonic()
        self._in_render = True
        self._stopped = self._stop_requested = False
        captured_serial = self._update_serial
        self._render_mode = True
        self.update_render_controls()
        try:
            # Camera notifications cannot change ROP devices, dimensions or
            # sampling controls. Reuse the accepted configuration rather than
            # rebuilding the GPU parameter layout on every navigation event.
            # ROP/source edits revoke the same camera-only guard used below.
            config = self.entry.config if self._camera_capture_allowed() else native_scene.configuration(self._rop)
            entry = registry.for_rop(self._rop, config)
            self._attach(entry)
            if entry.channel:
                for viewer in hou.ui.paneTabs():
                    if (
                        viewer.type() == hou.paneTabType.IPRViewer
                        and viewer.ropNode() == self._rop
                        and viewer.isActive()
                    ):
                        viewer.startRender()
                        return
            camera_only = self._camera_capture_allowed()
            serial = entry.change_serial
            capture_time = hou.time()
            narrow = None
            if not camera_only:
                # The same narrow rules as Houdini's Render View (Entry.narrow_update).
                try:
                    motion = native_scene.motion_configuration(self._rop, capture_time)
                except hou.Error:
                    motion = None
                if motion is not None:
                    narrow = entry.narrow_update(
                        capture_time, motion, native_scene.capture(self._rop, at_time=capture_time, camera_only=True)
                    )
            refresh_observer = bool(narrow and narrow.get("_refresh_observer"))
            receiver_linking = bool(narrow and narrow.get("_narrow") == "linking")
            update = (
                narrow
                if narrow is not None
                else native_scene.capture(
                    self._rop,
                    at_time=capture_time,
                    deleted_sources=entry.observer.deleted_sources,
                    camera_only=camera_only,
                )
            )
            # Keep pick data in viewer renders so arming/disarming the tool
            # never changes the scene. Transport these planes only on demand.
            if not camera_only and narrow is None:
                update["passes"] = list(dict.fromkeys(update["passes"] + ["position", "depth"]))
                if self.clay.isChecked():
                    from . import viewer_clay

                    update = viewer_clay.apply(update)
            entry.apply(
                update,
                capture_time,
                reuse_if_unchanged=reuse_if_unchanged,
                change_serial=serial if camera_only or narrow is not None else None,
            )
            self._update_clock.host_capture(host_started, time.monotonic())
            camera = hou.node(self._rop.evalParm("camera"))
            # Entry.apply already sampled and retained this exact signature.
            self._focus_capture = entry.last_camera if camera else None
            self._render_mode = True
            self._capture_timeline_complete(capture_time)
            self._pending = self._update_serial != captured_serial
            if (not camera_only and narrow is None) or refresh_observer:
                self._observer.refresh()
            elif receiver_linking and entry.change_serial == entry.applied_serial:
                self._observer.remember_parameters(
                    hou.nodeBySessionId(identity) for identity in entry.linking_values_pending
                )
        except Exception as error:
            self._render_mode = False
            self.info.setText("Render failed: " + str(error))
            if self.entry:
                self.entry.fail(error)
            if self._observer:
                self._observer.refresh()
        finally:
            self._in_render = False
            self.update_render_controls()

    def _observe_timeline(self, now):
        if now != self._last_time:
            self._last_time = now
            self._timeline_pending_time = now
            self.request_update(kind="frame")

    def _capture_timeline_complete(self, captured_time):
        # A later frame can arrive while a host capture is in progress. Keep
        # that pending request; the queued host callback will sample it next.
        if self._timeline_pending_time == captured_time:
            self._timeline_pending_time = None
        if self._timeline_pending_time is None:
            self._last_time = captured_time

    def _queue_automatic_update(self, now):
        if not (
            self._pending
            and self.auto.isChecked()
            and not self.entry.channel
            and not self._stopped
            and not self.entry.paused
            and not self.entry.final_paused
        ):
            return
        if not self.entry.defer_source_capture() and (
            self._timeline_pending_time is not None
            or self.entry.camera_only_pending
            # A narrow update is cheap: no gesture debounce.
            or self.entry.light_values_pending
            or self.entry.linking_values_pending
            or self.entry.material_values_pending
            or now - self._changed_at >= 0.08
            or now - self._pending_since >= 0.25
        ):
            # Parameter gestures retain their debounce. Timeline requests are
            # already coalesced by the single pending host render callback.
            self.queue_render(reuse_if_unchanged=True)

    def request_update(self, *, sync_controls=True, kind="edit"):
        if sync_controls:
            self.sync_preview_controls()
        now = time.monotonic()
        if not self._pending:
            self._pending_since = now
        self._changed_at = now
        self._update_serial += 1
        self._pending = True
        if hasattr(self, "_update_clock"):
            self._start_update_clock(kind)

    def _updates_automatically(self):
        entry = self.entry
        return bool(
            entry
            and self._render_mode
            and self.auto.isChecked()
            and not self._stopped
            and not entry.paused
            and not entry.final_paused
            and not entry.channel
            and self._final_view is None
            and self.ui_active()
        )

    def _start_update_clock(self, kind, *, restart=True, force=False):
        """The status time counts from here (see update_clock)."""
        if force or self._updates_automatically():
            self._update_clock.trigger(kind, restart=restart)

    def _update_waiting(self):
        """This viewer still holds an update that has not reached the session."""
        return bool(
            self._render_queued
            or self._in_render
            or self._camera_update_queued
            or self._pose_preview_queued
            or self._deferred_render
            or (self._pending and self._updates_automatically())
        )

    def _update_details(self, generation):
        """Transfer time of one generation, from the session's own event log (no host access)."""
        session = self.entry.session if self.entry else None
        controller = getattr(session, "_controller", None)
        if controller is None:
            return {}
        with controller.condition:
            events = [event for event in controller.events[-16:] if event.get("submission") == generation]
        if not events:
            return {}
        profile = events[-1].get("request_profile") or {}
        transfer = profile.get("exchange_seconds")
        if transfer is None:
            return {}
        return dict(transfer=float(transfer) + float(profile.get("snapshot_encode_seconds", 0.0)))

    def change_camera(self):
        if self._rop:
            self._rop.parm("camera").set(self.camera.currentData())
            self.request_update()

    def pause(self, paused):
        if self.entry:
            if hou.isUIAvailable() and self._final_view is None:
                for viewer in hou.ui.paneTabs():
                    if (
                        viewer.type() == hou.paneTabType.IPRViewer
                        and viewer.ropNode() == self._rop
                        and viewer.isActive()
                    ):
                        viewer.pauseRender() if paused else viewer.resumeRender()
            self.entry.session.pause(paused or self.entry.final_paused)
            self.entry.paused = paused
            self.update_render_controls()

    def cancel(self):
        self._deferred_render = False
        self._final_queued = False
        self._pose_preview_queued = False
        self._render_mode = False
        self._stopped = True
        self._pending = self._render_queued = False
        if self._final_view is not None and self._final_view.running:
            self._stop_requested = True
            self._final_view.cancel()
            return
        if self.entry:
            self._stopped = self._stop_requested = True
            self._pending = self._render_queued = False
            self.entry.stop_serial += 1
            self.entry.final_update_pending = False
            self.entry.session.cancel()
            self.entry.paused = False
            self.update_render_controls()
        else:
            self.update_render_controls()

    def set_region(self, crop):
        if self._final_view is not None:
            return
        if self.entry:
            try:
                reference = self.canvas.full_size if all(self.canvas.full_size) else None
                self.entry.set_crop_override(crop, reference)
                self.entry.apply({"crop": self.entry.crop_override}, hou.time())
                self.region.setChecked(True)
            except Exception as error:
                self.info.setText(str(error))

    def toggle_region(self, enabled):
        self.canvas.region_enabled = enabled
        if not enabled:
            self.reset_region()

    def reset_region(self):
        if self._final_view is not None:
            return
        self.region.blockSignals(True)
        self.region.setChecked(False)
        self.region.blockSignals(False)
        self.canvas.region_enabled = False
        if self.entry:
            width, height = self.entry.config[1:3]
            full_crop = (0, 0, width, height)
            had_region = (
                self.entry.crop_override is not None
                or tuple(self.entry.last_values.get("crop", full_crop)) != full_crop
            )
            self.entry.set_crop_override(None)
            if had_region:
                try:
                    self.entry.apply({"crop": full_crop}, hou.time())
                except Exception as error:
                    self.info.setText(str(error))

    def populate_views(self, display):
        policy = color_policy()
        self.view.blockSignals(True)
        self.view.clear()
        self.view.addItems(policy["views"].get(display, []))
        self.view.setCurrentText(policy["default_views"].get(display, ""))
        self.view.blockSignals(False)
        self.display_changed()

    def backgrounds_changed(self):
        """Background, checker or theme changed: reconvert the live frame for new composites."""
        if not hasattr(self, "_display_queue"):
            return
        if self.canvas.display_backgrounds() != getattr(self, "_requested_backgrounds", None):
            self.display_changed()

    def display_changed(self, *args):
        self._settings_dirty = True
        self._display_epoch += 1
        self._display_queue.follow(None, self._display_epoch, {}, None)
        if self.entry:
            self.follow_display(self.entry.session)
        self._frame_wakeup.notify()

    def refresh_color_policy(self):
        now = time.monotonic()
        if now - self._last_color_poll < 1:
            return
        self._last_color_poll = now
        policy = color_policy()
        if self.entry and self.entry.session.status()["state"] != "closed":
            self.entry.session.notify_color_configuration()
        if policy["configuration_id"] == self._color_configuration_id:
            return
        display, view = self.display.currentText(), self.view.currentText()
        self.display.blockSignals(True)
        self.view.blockSignals(True)
        self.display.clear()
        self.display.addItems(list(policy["views"]))
        self.display.setCurrentText(display if display in policy["views"] else policy["default_display"])
        display = self.display.currentText()
        choices = policy["views"].get(display, [])
        self.view.clear()
        self.view.addItems(choices)
        self.view.setCurrentText(view if view in choices else policy["default_views"].get(display, ""))
        self.display.blockSignals(False)
        self.view.blockSignals(False)
        self._color_configuration_id = policy["configuration_id"]
        self.display_changed()

    def poll_memory(self, force=False):
        if self._disposed:
            return
        enabled = self.memory_details.isChecked()
        self.memory_info.setVisible(enabled)
        if not enabled:
            return
        if self.entry is None:
            self.memory_info.setText("Mem: —", tooltip="No render session.")
            return
        # Invisible panes keep their last readout. Their ordinary timer picks
        # up current counters when shown, without polling the graphics driver.
        if not self.isVisible() or (not force and time.monotonic() - self._last_memory_poll < 1):
            return
        self._last_memory_poll = time.monotonic()
        try:
            status = self.entry.session.status()
            state = status.get("backend_state", status["state"]) if status.get("update_rejected") else status["state"]
            if state not in ("rendering", "paused", "complete"):
                self.memory_info.setText("Mem: —", tooltip="Render session is inactive.")
                return
            snapshot = process_memory()
            from .. import devices
            from . import viewer_memory

            selected = status.get("render_device_ids", ())
            renderer = self.entry.session.memory_snapshot()
            available = devices()
            details, scope = viewer_memory.describe(snapshot, renderer, selected, available)
            allocation = status.get("device_allocation_bytes")
            if isinstance(allocation, (int, float)):
                details += f"\nCycles tracked device allocations (this session): {allocation / 2**20:.1f} MiB"
                scope += (
                    " Cycles tracked allocations are not total GPU usage, physical residency, or free device memory."
                )
            self.memory_info.setText(
                viewer_memory.compact(snapshot, renderer, selected, available), tooltip=details + "\n\n" + scope
            )
        except Exception as error:
            self.memory_info.setText("Mem: N/A", tooltip="Process memory unavailable: " + str(error))

    def poll_texture_inputs(self, force=False):
        enabled = self.texture_details.isChecked()
        self.texture_info.setVisible(enabled)
        if not enabled or not self.entry:
            return
        status = self.entry.session.status()
        if status["state"] == "closed":
            self.texture_info.setPlainText("Render session closed")
            return
        revision = (id(self.entry.session), status["generation"], status["color_update_pending"])
        if not force and revision == self._texture_inputs_revision:
            return
        inputs = self.entry.session.texture_inputs()
        lines = []
        for item in inputs[:128]:
            lines.append(f"{item['filename']}\n  {item['colorspace'] or 'Engine Auto'} · {item['selection']}")
            if item.get("portable_decode_bytes"):
                mib = item["portable_decode_bytes"] / (1024 * 1024)
                storage = item.get("portable_cache_format")
                lines.append(
                    f"  Portable conversion: {mib:.1f} MiB full decode" + (f"; TX format: {storage}" if storage else "")
                )
        if len(inputs) > 128:
            lines.append(f"{len(inputs) - 128} additional textures.")
        if status["color_update_pending"]:
            lines.insert(0, "Previous render inputs; waiting for an update.")
        self.texture_info.setPlainText(("\n".join(lines) or "No file textures in the current render.")[:32000])
        self._texture_inputs_revision = revision

    @QtCore.Slot()
    def present_available(self):
        """Image-only GUI delivery; never pump capture, cook SOPs or inspect HOM.

        Both frame acceptance and conversion completion enqueue this bounded
        wakeup. The ordinary timer remains responsible for host updates,
        diagnostics and reference sessions that do not publish notifications.
        """
        if self._disposed or self._presenting or not self.entry or not self.ui_active():
            return
        self._presenting = True
        try:
            session = self.entry.session
            if self._session is not session:
                self._session = session
                self._sequence = 0
                self._settings_dirty = True
                self.watch_frames(session)
                self._display_epoch += 1
                self._display_queue.invalidate(None)
            # A pending edit retains the last accepted scene. Frame pixels,
            # logical generation and physical revision are captured together;
            # a concurrent preview/refinement cannot mislabel the owned image.
            generation, revision, owned = session.display_state(0 if self._settings_dirty else self._sequence)
            frame = None
            if owned:
                from .. import _native

                frame = _native.frame_info(owned[0])
            if frame:
                selected = self.pass_menu.currentData()
                enabled = frame.get("enabled_passes", frame["passes"])
                choices = [self.pass_menu.itemData(i) for i in range(self.pass_menu.count())]
                if choices != enabled:
                    self.pass_menu.blockSignals(True)
                    self.pass_menu.clear()
                    for name in enabled:
                        self.pass_menu.addItem(pass_label(name), name)
                    self.pass_menu.setCurrentIndex(max(0, self.pass_menu.findData(selected)))
                    self.pass_menu.blockSignals(False)
                    selected = self.pass_menu.currentData()
                selected = self._viewer_state.restore_pass(enabled, selected)
                self._comparison_available = (
                    "noisy_combined" in enabled and "combined" in enabled and frame.get("denoiser", "none") != "none"
                )
                if selected == "combined" and self._comparison_available and self.show_noisy.isChecked():
                    selected = "noisy_combined"
                self._displayed_pass = selected
                session.set_preview_pass(selected, focus=self._focus_transport())
                if selected in frame["passes"]:
                    descriptor = frame["pass_metadata"][selected]
                    self._displayed_channels = tuple(descriptor["channels"])
                    color = descriptor["color"]
                    alpha = "A" in descriptor["channels"]
                    self._pass_color = color
                    self._pass_alpha = alpha
                    if not alpha and self.channels.currentData() == "alpha":
                        self.channels.blockSignals(True)
                        self.channels.setCurrentIndex(self.channels.findData("rgb"))
                        self.channels.blockSignals(False)
                else:
                    self.info.setText("Waiting for the selected pass to update")
                self.update_display_controls()
            self.follow_display(session)
            converted = self._display_queue.take()
            # The worker may have committed another physical revision during
            # conversion. Retain the displayed image, but retire obsolete work.
            current = session.display_state(None)[:2]
            if (
                converted
                and converted[0][0] == self._display_epoch
                and converted[0][2:] == current
                and self._display_queue.is_current(converted[0])
            ):
                _, image, error = converted
                if error:
                    raise RuntimeError(error)
                if image is not None:
                    self._previous_color_notice = image.get("previous_color_notice", "")
                    previous_view = (self.canvas.full_size, self.canvas.pixel_aspect())
                    size = image["full_width"], image["full_height"]
                    storage = image.get("buffer_width", image["width"]), image.get("buffer_height", image["height"])
                    merged_from = None if self._live_image is None else self._live_image.cacheKey()
                    # Tile upload is valid only when the prior buffer was a
                    # full raster of this size. Compact images replace it.
                    damage_from = (
                        merged_from
                        if self._live_image is not None
                        and self._live_image.size().toTuple() == ImageCanvas.logical_size(self._live_image) == size
                        and "buffer_width" not in image
                        else None
                    )
                    self._live_image = ImageCanvas.merge_frame(self._live_image, image)
                    self._merge_live_composites(merged_from, image)
                    self._live_generation = image["generation"]
                    self._live_frame_metadata = {
                        name: image[name]
                        for name in (
                            "generation",
                            "sequence",
                            "rendered_width",
                            "rendered_height",
                            "samples",
                            "published_seconds",
                        )
                    }
                    self._live_frame_metadata["render_revision"] = image.get("render_revision", image["generation"])
                    self._live_frame_metadata["display_epoch"] = converted[0][0]
                    self._live_frame_metadata["buffer_width"], self._live_frame_metadata["buffer_height"] = storage
                    if self.displaying_live():
                        self.canvas.image = self._live_image
                        self.canvas.full_size = size
                        self.canvas.generation = image["generation"]
                        self.canvas.frame_metadata = dict(self._live_frame_metadata)
                        # A GPU canvas then updates only the merged tile of its texture.
                        self.canvas.note_damage(
                            damage_from,
                            self._live_image.cacheKey(),
                            (
                                image["x"],
                                image["full_height"] - image["y"] - image["height"],
                                image["width"],
                                image["height"],
                            ),
                        )
                        self.canvas.update()
                        if previous_view != (self.canvas.full_size, self.canvas.pixel_aspect()):
                            self.canvas.viewChanged.emit()
                    self._sequence = image["sequence"]
                    self._settings_dirty = False
        except Exception as error:
            self.info.setText("Render error: " + str(error))
        finally:
            self._presenting = False

    def set_host_pane_tab(self, pane_tab):
        """The Python Panel tab holding this viewer, so a hidden viewer can notice it was closed."""
        self._host_tab = pane_tab
        if self._lifetime.state == viewer_lifetime.HIDDEN and not self._liveness_timer.isActive():
            self._schedule_host_check()

    def ui_active(self):
        lifetime = getattr(self, "_lifetime", None)
        return lifetime is None or lifetime.state == viewer_lifetime.VISIBLE

    def showEvent(self, event):
        super().showEvent(event)
        handle = self.windowHandle() if self.internalWinId() else None
        if handle is not None and handle is not self._expose_watched:
            # The native viewer root: draw the canvas when it is exposed again
            # (also when the canvas comes back from image-only fullscreen).
            self._expose_watched = handle
            handle.installEventFilter(self._expose_repaint)
        self.update_lifetime()

    def _host_pane_tabs(self):
        tabs = list(hou.ui.paneTabs())
        for floating in hou.ui.floatingPanels():
            tabs.extend(floating.paneTabs())
        return tabs

    def _find_host_tab(self):
        if not hou.isUIAvailable():
            return None
        return viewer_lifetime.find_pane_tab(self._host_pane_tabs(), self, hou.paneTabType.PythonPanel, (hou.Error,))

    def _retry_revive(self):
        self._revive_posted = False
        if self._disposed and viewer_lifetime.canvas_visible(self.canvas):
            self._revive()

    def _revive(self):
        """Houdini shows this released viewer again: ask its tab for a fresh interface.

        Changing a tab from the viewer to another type and back re-shows the
        old widget without onCreateInterface. Reloading runs onDestroyInterface
        and onCreateInterface, which builds a new, working viewer. While the
        tab is still being set up it may not report this widget yet (or report
        a transient one), so this retries briefly while the widget stays on
        screen; nothing else wakes a released viewer.
        """
        if self._revive_posted:
            return
        self._revive_posted = True
        self._revive_attempts = getattr(self, "_revive_attempts", 0) + 1
        if self._revive_attempts > 30:
            self._revive_result = "attempts exhausted"
            self.info.setText("Cycles Render could not reopen. Close and reopen this pane tab.")
            session_log.write("viewer could not reopen", attempts=self._revive_attempts - 1)
            return
        tab = self._find_host_tab()
        if tab is None:
            QtCore.QTimer.singleShot(100, self._retry_revive)
            return
        widget = self

        def reload():
            try:
                if tab.activeInterfaceRootWidget() is widget:
                    tab.reloadActiveInterface()
                    widget._revive_result = "reloaded"
                    return
            except hou.Error:
                pass  # A different pane tab may now own this widget.
            if shiboken6.isValid(widget):
                widget._revive_result = "tab not ready"
                QtCore.QTimer.singleShot(100, widget._retry_revive)

        hou.ui.postEventCallback(reload)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.update_lifetime()
        if not self.timer.isActive():
            session_log.stall_monitor().rest(id(self))  # not polling on purpose: not a stall

    def eventFilter(self, watched, event):
        # In image-only fullscreen the canvas has its own window, which can be
        # minimised or restored while this panel's visibility does not change.
        if watched is getattr(self, "canvas", None) and event.type() in (QtCore.QEvent.Show, QtCore.QEvent.Hide):
            self.update_lifetime()
        return super().eventFilter(watched, event)

    def update_lifetime(self):
        """Apply a visibility change; also called from the pane tab's (de)activation hooks."""
        lifetime = getattr(self, "_lifetime", None)
        if lifetime is None:
            return
        if self._disposed:
            if viewer_lifetime.canvas_visible(self.canvas):
                self._revive()
            return
        change = lifetime.update(viewer_lifetime.canvas_visible(self.canvas))
        if change == "hide":
            self._enter_hidden()
        elif change == "show":
            self._enter_visible()

    def _enter_hidden(self):
        """Stop all UI work. The render follows viewer_lifetime.HIDDEN_RENDER.

        With the 16 ms poll stopped, scene edits only mark the shared entry
        changed; nothing captures or restarts the render until the viewer is
        shown again (or another visible viewer of the same ROP applies them).
        """
        self.timer.stop()
        rest = getattr(self, "_stall_rest", None)  # absent in tests that lift this method out
        if rest is not None:
            rest()  # not polling on purpose: not a stall
        self._resume_timer.stop()
        self._shortcuts.set_enabled(False)
        entry = self.entry
        if entry is not None:
            entry.session.set_feedback(id(self), False)
            if (
                self._lifetime.policy == "pause"
                and self._final_view is None
                and entry.views == 1
                and not entry.channel
                and not entry.paused
                and not entry.final_paused
            ):
                entry.session.pause(True)
                self._hidden_paused = entry
        # Worker frame notifications start display conversions; unsubscribe.
        self.watch_frames(None)
        self._schedule_host_check()

    def _enter_visible(self):
        self._liveness_timer.stop()
        self._shortcuts.set_enabled(True)
        paused, self._hidden_paused = self._hidden_paused, None
        if paused is not None and not paused.paused:
            paused.session.pause(paused.final_paused)
        if self.entry is not None:
            self.watch_frames(self.entry.session)
        # Show the current state at once: the next poll refreshes status text,
        # memory and the image, and applies scene edits made while hidden.
        self._status_key = None
        self._status_due = 0.0
        self._last_memory_poll = 0
        self.timer.start()
        self._resume_timer.start()

    def _resume_poll(self):
        if not self._disposed and self.ui_active():
            self.poll()

    def _schedule_host_check(self):
        if self._host_tab is None or self._disposed:
            return
        delay = self._lifetime.next_check_ms()
        if delay is not None:
            self._liveness_timer.start(delay)

    def check_host(self):
        """Dispose a hidden viewer whose pane tab can no longer show it."""
        if self._disposed or self._lifetime.state != viewer_lifetime.HIDDEN:
            return
        if viewer_lifetime.canvas_visible(self.canvas):
            self.update_lifetime()  # Shown without a show event reaching us: never release.
            return
        if not viewer_lifetime.pane_tab_holds(self._host_tab, hou.paneTabType.PythonPanel, (hou.Error,)):
            # The tab captured at creation can be stale (a tab whose type was
            # changed to the viewer). Adopt the tab that shows this widget now.
            tab = self._find_host_tab()
            if tab is None:
                self.dispose()
                return
            self._host_tab = tab
        self._schedule_host_check()

    def _stall_beat(self):
        # The session log's stall monitor watches these beats from its own thread: a GUI thread that does
        # not come back to this 16 ms poll for STALL_SECONDS is recorded, with its Python stack, while blocked.
        self._stall_poll_started = time.monotonic()
        session_log.stall_monitor().beat(id(self))

    def _stall_rest(self):
        session_log.stall_monitor().rest(id(self))

    def _stall_after(self):
        monitor = session_log.stall_monitor()
        started = getattr(self, "_stall_poll_started", None)
        if started is not None:
            monitor.poll_took(id(self), time.monotonic() - started)
        if self._disposed or not self.timer.isActive():
            monitor.rest(id(self))

    def poll(self):
        if self._disposed:
            return
        if not self.ui_active():
            self.timer.stop()  # Shown again: _enter_visible restarts it.
            return
        if self._deferred_render and not registry.local_final_active():
            self._deferred_render = False
            if not self._stopped:
                self.queue_render()
        if time.monotonic() >= self._next_scale_poll:
            self._next_scale_poll = time.monotonic() + 0.25
            self.monitor_scale.update()
        self.poll_memory()
        self.finish_pending_save()
        try:
            # Native IPR may start after the panel selected its ROP, or replace
            # the session during scene reload. Joining retains its generation.
            if self._final_view is not None:
                self._final_view.poll()
                return
            self._attach_active()
            if not self.entry:
                self.update_render_controls()
                return
            self.refresh_color_policy()
            if self._pending_pick is not None:
                self._resolve_pending_pick()
            status = self.entry.session.status()
            if self.entry.observer:
                self.entry.observer.refresh_material_bindings(self.entry.session)
            if self._observer:
                self._observer.refresh_material_bindings(self.entry.session)
            self.entry.session.set_feedback(id(self), self.canvas.isVisible() and self.displaying_live())
            if (
                status.get("backend_state", status["state"]) if status.get("update_rejected") else status["state"]
            ) not in ("rendering", "paused"):
                self._stop_requested = False
            self.update_render_controls(status)
            if status["state"] == "closed":
                self.info.setText("Render session closed")
                return
            self.poll_texture_inputs()
            if self._session is not self.entry.session:
                self._session = self.entry.session
                self._sequence = 0
                self._settings_dirty = True
                self.watch_frames(self.entry.session)
                self._display_epoch += 1
                self._display_queue.invalidate(None)
                self._update_clock.rebase()
            self._update_clock.observe(status, waiting=self._update_waiting(), details=self._update_details)
            self._observe_timeline(hou.time())
            # Native IPR can apply the edit before this panel's Qt timer runs.
            # Consume that acknowledgement so a stale local notification cannot
            # restart rendering when the artist subsequently clicks Stop.
            if (
                self._pending
                and self.entry.change_serial == self.entry.applied_serial
                and hou.time() == self.entry.observed_time
            ):
                self._pending = False
                self._timeline_pending_time = None
            if (
                self.entry.change_serial > self.entry.applied_serial
                and self.entry.requested_serial != self.entry.change_serial
            ):
                # Do not restart the debounce every poll of a registry event.
                if not self._pending:
                    self.request_update()
            if self.entry.pose_serial > max(self.entry.applied_serial, self.entry.pose_preview_serial):
                self.queue_pose_preview()
            self._queue_automatic_update(time.monotonic())
            self.present_available()
            self.update_save_control(status)
            # Elapsed time, progress and sample counters change on every 16 ms
            # poll while rendering. Each label change repaints and re-lays out
            # the status row, and when docked that work is flushed through
            # Houdini's main window, which also hosts the scene viewport.
            # Refresh those counters at most every STATUS_REFRESH_SECONDS;
            # state changes, errors and text set elsewhere still apply at once.
            now = time.monotonic()
            update = self._update_clock.current
            status_key = (
                status["state"],
                status.get("backend_state"),
                bool(status.get("update_rejected")),
                self.entry.error,
                status["error"],
                # A new update or its start of rendering shows at once.
                (id(update), update.open, update.bound) if update is not None else None,
            )
            if (
                status_key == self._status_key
                and now < self._status_due
                and getattr(self.info, "_text_key", None) == self._status_applied
            ):
                return
            self._status_key = status_key
            self._status_due = now + STATUS_REFRESH_SECONDS
            message = self.entry.error or status["error"] or status["message"]
            from .viewer_timing import text as timing_text
            from .update_clock import tooltip as timing_tooltip

            # The time shown counts from the artist's action (update_clock);
            # the renderer's own sampling time stays in the tooltip.
            timing = self._update_clock.view(status, now)
            shown_seconds = timing["total"] if timing else status.get("elapsed_seconds", 0)
            if status.get("update_rejected"):
                headline = f"Edit rejected · {status['error']} · Last accepted scene {status['backend_state']}"
            elif status["state"] == "failed":
                from .viewer_timing import failure_headline

                headline = failure_headline(timing_text(shown_seconds), message)
            elif timing and timing["open"] and timing["phase"] == "loading":
                from .viewer_timing import loading_headline

                headline = loading_headline(timing_text(shown_seconds), message)
            else:
                headline = (
                    f"{status['state'].title()} · {status['progress']:.1%} · {timing_text(shown_seconds)} · {message}"
                )
            issues = status.get("source_diagnostics", [])
            source_warnings = sum(str(issue.get("severity", "warning")).lower() != "info" for issue in issues)
            if source_warnings:
                headline += f" - {source_warnings} source warning(s)"
            if status.get("motion_warnings"):
                headline += " · Warning: " + " · ".join(status["motion_warnings"])
            # Engine render warnings (e.g. Nested Dielectrics): the status line only, never the Houdini console.
            from ..core import render_warnings

            headline += render_warnings.headline(status)
            if self._previous_color_notice:
                headline += " · " + self._previous_color_notice
            if self._notice is not None:
                if time.monotonic() < self._notice[1]:
                    headline = self._notice[0] + " · " + headline
                else:
                    self._notice = None
            diagnostic = (
                headline
                + "\n"
                + f"First image {status['first_image_seconds']:.3f}s · cook {status['cooking_seconds']:.3f}s · "
                f"extract {status['extraction_seconds']:.3f}s · buffers {status['device_allocation_bytes'] / 2**20:.1f} MiB · "
                f"geometry conversions {status['geometry_conversions']}"
                + (" · moving volumes: ray marching" if status.get("volume_ray_marching") else "")
                + (
                    {
                        "oidn_gpu": " · OpenImageDenoise (render GPU)",
                        "oidn_cpu": " · OpenImageDenoise (CPU)",
                        "dlss": " · DLSS Ray Reconstruction",
                        "optix": " · OptiX denoising",
                    }.get(status.get("denoiser"), "")
                )
            )
            from .viewer_display import describe

            tooltip = (
                diagnostic
                + ("\n\n" + message if status["state"] == "failed" and message else "")
                + "\n"
                + timing_tooltip(timing, status.get("elapsed_seconds", 0))
                + "\n"
                + describe(self.canvas)
                + (
                    "\n\n"
                    + "\n\n".join(f"{issue['source']}\n{issue['message']}\n{issue['action']}" for issue in issues)
                    if issues
                    else ""
                )
            )
            self.info.setText(diagnostic, tooltip=tooltip)
            self.viewer_status.update_status(status)
            self._status_applied = getattr(self.info, "_text_key", None)
        except Exception as error:
            self.info.setText("Render error: " + str(error))

    def _live_over_black(self, image):
        """JPEG has no alpha: the live frame over black in scene-linear light, View(RGB_premult).

        A snapshot, a view without composites (RGB, R, G, B, Alpha, data) or a
        conversion failure keeps image; viewer_save then composites it on black
        in display values, as before.
        """
        if not self.entry or self._live_image is None or self.canvas.image.cacheKey() != self._live_image.cacheKey():
            return image
        settings = dict(self.display_options(), backgrounds=((0, 0, 0),))
        if settings["channels"] == "rgb" and settings.get("use_alpha", False):
            settings["channels"] = "rgba"
        try:
            result = self.entry.session.display_image(**settings)
        except Exception:
            return image
        if (
            not result
            or not result.get("composites")
            or result["generation"] != self._live_generation
            or (result["full_width"], result["full_height"]) != image.size().toTuple()
        ):
            return image
        base = QtGui.QImage(image.size(), QtGui.QImage.Format_RGB32)
        base.fill(QtGui.QColor("black"))
        painter = QtGui.QPainter(base)
        painter.drawImage(0, 0, image)  # Outside a render region: as before.
        painter.end()
        base = base.convertToFormat(QtGui.QImage.Format_RGBA8888)
        return ImageCanvas.merge_frame(base, dict(result, rgba=result["composites"][0]))

    def _merge_live_composites(self, merged_from, image):
        """Merge the converted composites like the straight image; register them under its new key."""
        registry = self.canvas.composites
        previous = registry.pop(merged_from, None) if merged_from is not None else None
        composites = image.get("composites")
        if not composites:
            return
        backgrounds = image["backgrounds"]
        if previous is not None and (previous[0] != backgrounds or len(previous[1]) != len(composites)):
            previous = None  # Other backgrounds: start transparent (the straight blend shows there).
        merged = [
            ImageCanvas.merge_frame(
                previous[1][index] if previous is not None else None,
                dict(image, rgba=composite),
                QtCore.Qt.transparent,
            )
            for index, composite in enumerate(composites)
        ]
        registry[self._live_image.cacheKey()] = (backgrounds, merged)

    def snapshot(self):
        if self.canvas.image.isNull():
            return
        self.add_history(self.canvas.image, f"Snapshot {time.strftime('%H:%M:%S')}")

    def add_history(self, source, label):
        # Reject oversized images before making a full-resolution copy.
        width, height = ImageCanvas.logical_size(source)
        image_bytes = source.sizeInBytes() if source.size().toTuple() == (width, height) else width * height * 4
        if image_bytes > 64 * 2**20:
            self.notify("Image exceeds the 64 MiB render-history budget")
            return
        image = ImageCanvas.full_resolution_copy(source)
        # Keep the composites made over the backgrounds of this moment; they are
        # shown only while the viewer has the same backgrounds (see composite_images).
        registry = self.canvas.composites
        entry = registry.get(source.cacheKey())
        if entry is not None and image_bytes * (1 + len(entry[1])) <= 64 * 2**20:
            registry[image.cacheKey()] = (entry[0], [ImageCanvas.full_resolution_copy(c) for c in entry[1]])

        def stored(item):
            extra = registry.get(item.cacheKey())
            return item.sizeInBytes() + (sum(c.sizeInBytes() for c in extra[1]) if extra else 0)

        previous = selected = self.snapshot_menu.currentIndex()
        self.snapshot_menu.blockSignals(True)
        self._snapshots.append(image)
        self.snapshot_menu.addItem(label)
        while len(self._snapshots) > 8 or sum(stored(i) for i in self._snapshots) > 64 * 2**20:
            registry.pop(self._snapshots.pop(0).cacheKey(), None)
            self.snapshot_menu.removeItem(1)
            if selected:
                selected = max(0, selected - 1)
        self.snapshot_menu.setCurrentIndex(selected)
        self.snapshot_menu.blockSignals(False)
        self.update_history_controls()
        if previous and not selected:
            self.show_snapshot(0)

    def select_history(self, value):
        self.snapshot_menu.setCurrentIndex(0 if value == self.history_slider.maximum() else value)

    def update_history_controls(self):
        index = self.snapshot_menu.currentIndex()
        count = len(self._snapshots)
        self.history_slider.blockSignals(True)
        self.history_slider.setRange(1, count + 1)
        self.history_slider.setValue(index or count + 1)
        self.history_slider.setEnabled(count > 0)
        self.history_slider.blockSignals(False)
        if hasattr(self, "snapshot_compare"):
            self.snapshot_compare.setEnabled(count > 0)
        self.history_number.setText(f"{index}/{count}" if index else "Live")
        # Reserve the number's width so changing its text cannot shift controls.
        self.history_number.setFixedWidth(
            max(self.history_number.fontMetrics().horizontalAdvance(text) for text in ("8/8", "Live"))
        )
        self.update_display_controls()

    def update_display_controls(self):
        # Returning to Live restores controls immediately, without waiting for
        # a new framebuffer event. Retain each pass's raw/alpha restrictions.
        live = self.displaying_live()
        self.focus_pick.setEnabled(live and not self.snapshot_compare.isChecked() and self._final_view is None)
        self.canvas.set_focus_enabled(self.focus_pick.isEnabled() and self.focus_pick.isChecked())
        self.pass_menu.setEnabled(live)
        self.show_noisy.setEnabled(live and self._comparison_available and self.pass_menu.currentData() == "combined")
        self.channels.setEnabled(self._pass_color and live)
        self.channel_alpha.setEnabled(
            self._pass_color and self._pass_alpha and live and self.channels.currentData() in ("rgb", "r", "g", "b")
        )
        self.channels.model().item(self.channels.findData("alpha")).setEnabled(self._pass_color and self._pass_alpha)
        self.sync_channel_buttons()
        color = self._pass_color and self.channels.currentData() != "alpha"
        for control in (self.exposure, self.gamma, self.exposure_label, self.gamma_label, self.display, self.view):
            control.setEnabled(color and live)
        self.reset_display.setEnabled(color and live)

    def show_snapshot(self, index):
        compare = hasattr(self, "snapshot_compare") and self.snapshot_compare.isChecked()
        self.canvas.compare_image = (
            self._snapshots[(index or len(self._snapshots)) - 1] if compare and self._snapshots else QtGui.QImage()
        )
        image = self._snapshots[index - 1] if index and not compare else self._live_image
        if image is not None:
            self.canvas.image = image
            self.canvas.full_size = ImageCanvas.logical_size(image)
            self.canvas.update()
            self.update_view_controls()
        if not index or compare:
            self.canvas.generation = self._live_generation
            # Live may have advanced while a snapshot stayed selected. Restore
            # the identity belonging to these exact cached pixels, even if the
            # renderer has finished and will never publish another frame.
            self.canvas.frame_metadata = dict(self._live_frame_metadata)
        self.update_history_controls()
        self.update_save_control()

    def update_save_control(self, status=None):
        if not hasattr(self, "save_format"):
            return
        status = status or (self.entry.session.status() if self.entry else {})
        ready = self.entry is not None and self.displaying_live() and self._pending_save is None
        if self.save_format.currentData() == "exr":
            ready = ready and status.get("state") == "complete"
        else:
            ready = ready and not self.canvas.image.isNull()
        self.save_button.setEnabled(bool(ready))

    def open_fullscreen(self):
        if self._disposed:
            return
        if self._fullscreen is not None:
            self.close_fullscreen()
            return
        from .viewer_fullscreen import ImageOnlyWindow, exit_host_fullscreen, keep_composited

        # A native Houdini fullscreen panel already has an owner. The same
        # toggle exits it rather than stacking a second fullscreen window.
        if exit_host_fullscreen(self):
            return
        screen = QtWidgets.QApplication.screenAt(self.canvas.mapToGlobal(self.canvas.rect().center())) or self.screen()
        window = ImageOnlyWindow(self)
        window.setWindowTitle("Cycles Image — Escape to return")
        window.closing.connect(self.restore_fullscreen)
        self._fullscreen = window
        window.winId()
        window.windowHandle().installEventFilter(self._expose_repaint)
        if screen is not None:
            window.windowHandle().setScreen(screen)
            window.setGeometry(screen.geometry())
        window.showFullScreen()
        window.composited = keep_composited(window)
        self.canvas.setFocus()
        self.canvas.display_density_changed()

    def close_fullscreen(self):
        if self._fullscreen is not None:
            self._fullscreen.close()
        elif not self._disposed:
            from .viewer_fullscreen import exit_host_fullscreen

            exit_host_fullscreen(self)

    def restore_fullscreen(self):
        window = self._fullscreen
        if window is None:
            return
        self._fullscreen = None
        window.restore()
        self.canvas.update()  # Its last frame went to the fullscreen window.
        self.canvas.setFocus()
        self.canvas.display_density_changed()
        self.update_view_controls()
        window.deleteLater()

    def image_output_directory(self):
        from pathlib import Path

        if self._rop is None:
            raise ValueError("Select a Cycles ROP first")
        saved = self._saved_output_directories.get(self._rop.sessionId())
        if saved is not None:
            return saved
        picture = self._rop.parm("picture").evalAtTime(hou.time()).strip()
        if not picture:
            raise ValueError("Set Output Picture on the selected ROP")
        return Path(picture).expanduser().absolute().parent

    def open_output_folder(self):
        try:
            directory = self.image_output_directory()
            if not directory.is_dir():
                raise ValueError(f"Output folder does not exist yet: {directory}")
            if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))):
                raise ValueError(f"Could not open output folder: {directory}")
        except (ValueError, OSError, hou.Error) as error:
            self.notify(str(error))

    def notify(self, text, seconds=8.0):
        # The status headline refreshes every poll. Keep transient results
        # (saves, focus picks, rejected inputs) visible for a few seconds.
        self._notice = (text, time.monotonic() + seconds)
        self.info.setText(text)

    def save_exr(self):
        if not self.entry or self._pending_save is not None:
            return
        try:
            from datetime import datetime
            from pathlib import Path
            from uuid import uuid4
            from ..rop import output
            from . import viewer_save

            rop = self._rop
            if rop is None:
                raise ValueError("Select a Cycles ROP first")
            picture = rop.parm("picture").evalAtTime(hou.time()).strip()
            if not picture:
                raise ValueError("Set Output Picture on the selected ROP")
            base = Path(picture).expanduser().absolute()
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f") + "_" + uuid4().hex[:8]
            kind = self.save_format.currentData()
            target = base.with_name(base.stem + "_" + stamp + "." + kind)
            if kind == "exr":
                jobs = output.plan(rop, hou.time(), str(target))
                # Explicit per-pass output paths may not use the main filename.
                # Make every resulting output unique, including those paths.
                for job in jobs:
                    path = Path(job["filename"])
                    if stamp not in path.name:
                        path = path.with_name(path.stem + "_" + stamp + path.suffix)
                    job["filename"] = str(path)
                event = viewer_save.begin_exr(self.entry.session, jobs)
                filename = jobs[0]["filename"]
            else:
                image = ImageCanvas.full_resolution_copy(self.canvas.image)
                if image.isNull():
                    raise ValueError("No image is available to save")
                if kind == "jpg":
                    image = self._live_over_black(image)
                event = viewer_save.begin_image(image, str(target), kind)
                filename = str(target)
            self._pending_save = dict(
                event=event, filename=filename, kind=kind, rop=rop.sessionId(), started=time.monotonic()
            )
            self.save_button.setEnabled(False)
            self.notify("Saving " + kind.upper() + ": " + filename, seconds=float("inf"))
        except Exception as error:
            self._pending_save = None
            self.notify("Output failed: " + str(error))

    def _record_saved(self, rop, filename):
        from pathlib import Path

        if rop is not None:
            self._saved_output_directories[rop.sessionId()] = Path(filename).absolute().parent

    def finish_pending_save(self):
        pending = self._pending_save
        if pending is None:
            return
        try:
            if not pending["event"].is_set():
                return
            self._pending_save = None
            if pending["event"].error:
                raise pending["event"].error
            if pending["rop"] is not None:
                from pathlib import Path

                self._saved_output_directories[pending["rop"]] = Path(pending["filename"]).absolute().parent
            self.notify("Saved " + pending["kind"].upper() + ": " + pending["filename"])
        except Exception as error:
            self._pending_save = None
            self.notify("Output failed: " + str(error))

    def dispose(self):
        if self._locked_camera:
            self.lock_camera(False)
        if not self._disposed:
            self._resolution_linker.close()
            self._viewer_state.close()
            self._disposed = True
            self.timer.stop()
            session_log.stall_monitor().rest(id(self))
            session_log.write("viewer closed")
            self._lifetime.close()
            self._liveness_timer.stop()
            self._resume_timer.stop()
            self._host_tab = None
            paused, self._hidden_paused = self._hidden_paused, None
            if paused is not None and not paused.paused:
                paused.session.pause(paused.final_paused)
            self._shortcuts.close()
            self.background_button.cancel_picker()
            self._frame_wakeup.close()
            self._display_queue.close()
            self.close_fullscreen()
            if hou.isUIAvailable():
                hou.ui.removePostedEventCallback(self._run_queued_render)
                hou.ui.removePostedEventCallback(self._run_queued_pose_preview)
                hou.ui.removePostedEventCallback(self._run_final_render)
            self._final_queued = False
            self._pose_preview_queued = False
            self._camera_update_queued = False
            self.timer.timeout.disconnect(self.poll)
            if self._observer:
                self._observer.close()
            self._detach()
            if self._final_view is not None:
                self._final_view.close()
                self._final_view = None
            # A Qt wrapper can outlive Houdini's Python Panel and interpreter teardown.
            # Release host nodes, native capsules and cached images while the host is alive.
            self._observer = None
            self._rop = None
            self._rop_identity = None
            self._session = None
            self._live_image = None
            self._snapshots.clear()
            self.canvas.composites.clear()
            self._live_frame_metadata.clear()
            self.canvas.image = QtGui.QImage()
            self.canvas.compare_image = QtGui.QImage()
            self.canvas.full_size = (0, 0)
            # GPU canvas: textures and shaders, destroyed in their own context.
            self.canvas.release_display()

    def closeEvent(self, event):
        self.dispose()
        super().closeEvent(event)
