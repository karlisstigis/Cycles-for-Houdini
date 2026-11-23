# SPDX-License-Identifier: Apache-2.0
"""One fullscreen owner, with delayed edge-revealed viewer controls."""

import hou
from PySide6 import QtCore, QtGui, QtWidgets


def floating_panel(panel):
    if not hou.isUIAvailable():
        return None
    for floating in hou.ui.floatingPanels():
        if any(
            tab.type() == hou.paneTabType.PythonPanel and tab.activeInterfaceRootWidget() is panel
            for tab in floating.paneTabs()
        ):
            return floating
    return None


def exit_host_fullscreen(panel):
    floating = floating_panel(panel)
    if floating is not None:
        if floating.isFullscreen():
            floating.setIsFullscreen(False)
            return True
        return False
    if hou.isUIAvailable() and panel.window() == hou.qt.mainWindow() and hou.ui.isFullscreen():
        hou.ui.setFullscreen(False)
        return True
    # Standalone Qt hosts have no HOM fullscreen owner.
    if panel.window().isFullScreen():
        panel.window().showNormal()
        return True
    return False


class ExposeRepaint(QtCore.QObject):
    """Draw the canvas again whenever a window holding it is exposed.

    The canvas is an ordinary child of a native window (the docked viewer
    root, or the image-only fullscreen window). When it moves into another
    window, its last frame was drawn before that window was on screen, and
    an OpenGL canvas's frame is composed into a window only by a canvas paint.
    With a finished render no new frame comes, so the window stayed black
    until another widget there repainted (revealing an edge toolbar). One
    update per expose, nothing per frame, nothing while hidden.
    """

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Expose and watched.isExposed():
            self.canvas.update()
        return False


def keep_composited(window):
    """Windows: give a fullscreen window a 1 px border so DWM keeps composing it.

    A canvas drawn with OpenGL makes its whole top-level window OpenGL based.
    Windows stops composing (DWM) an OpenGL window with exactly the screen's
    size: its content can stay black or flicker, and menus, tooltips and
    other windows do not show over it (Qt for Windows, "Fullscreen OpenGL
    Based Windows"). Qt's remedy is WS_BORDER, as setHasBorderInFullScreen
    does; that API is not reachable from Python, so set the style natively.
    Returns True when the border is set.
    """
    import sys

    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
    user32.SetWindowPos.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    )
    gwl_style, ws_border = -16, 0x00800000
    frame_changed = 0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010  # FRAMECHANGED, NOSIZE, NOMOVE, NOZORDER, NOACTIVATE
    handle = ctypes.c_void_p(int(window.winId()))
    style = user32.GetWindowLongPtrW(handle, gwl_style)
    if not style & ws_border:
        user32.SetWindowLongPtrW(handle, gwl_style, style | ws_border)
        user32.SetWindowPos(handle, None, 0, 0, 0, 0, frame_changed)
    return bool(user32.GetWindowLongPtrW(handle, gwl_style) & ws_border)


class ImageOnlyWindow(QtWidgets.QWidget):
    closing = QtCore.Signal()

    def __init__(self, panel):
        super().__init__(panel, QtCore.Qt.Window | QtCore.Qt.FramelessWindowHint)
        self.panel = panel
        self._cycles_shortcut_owner = panel
        self._restored = False
        self.setProperty("cycles_render_panel", True)
        self.setMouseTracking(True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        root = panel.layout()
        rows = panel.all_toolbar_rows
        widgets = [*rows[:2], panel.canvas, *rows[2:], panel.viewer_status, panel.texture_info]
        self.saved = [(root.indexOf(widget), widget, root.stretch(root.indexOf(widget))) for widget in widgets]
        self.top = self.overlay(rows[:2])
        self.bottom = self.overlay((*rows[2:], panel.viewer_status, panel.texture_info))
        root.removeWidget(panel.canvas)
        layout.addWidget(panel.canvas)
        panel.canvas.show()
        self.top.hide()
        self.bottom.hide()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(75)
        self.timer.timeout.connect(self.track_pointer)
        self.hide_timer = QtCore.QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(450)
        self.hide_timer.timeout.connect(self.hide_controls)
        self.app = QtWidgets.QApplication.instance()
        self.app.installEventFilter(self)
        self.timer.start()

    def overlay(self, widgets):
        result = QtWidgets.QWidget(self)
        result.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        result.setProperty("cycles_toolbar", True)
        layout = QtWidgets.QVBoxLayout(result)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        root = self.panel.layout()
        # Capture all indices before removing any sibling from the panel.
        saved = [(widget, widget.isHidden()) for widget in widgets]
        for widget, hidden in saved:
            root.removeWidget(widget)
            layout.addWidget(widget)
            widget.setVisible(not hidden)
        return result

    def place_overlays(self):
        for overlay, bottom in ((self.top, False), (self.bottom, True)):
            height = overlay.layout().sizeHint().height()
            overlay.setGeometry(0, self.height() - height if bottom else 0, self.width(), height)
            overlay.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "top"):
            self.place_overlays()

    def reveal(self, overlay):
        self.hide_timer.stop()
        other = self.bottom if overlay is self.top else self.top
        if not self.busy_controls():
            other.hide()
        if not overlay.isVisible():
            overlay.show()
            self.place_overlays()

    def busy_controls(self):
        popup = self.app.activePopupWidget()
        if popup is not None:
            # Keep our menus open without pinning chrome for another app panel.
            while popup is not None:
                if popup is self or popup is self.panel:
                    return True
                popup = popup.parentWidget()
        return self.panel.background_button.dialog is not None or self.app.mouseButtons() != QtCore.Qt.NoButton

    def track_pointer(self):
        if self._restored:
            return
        point = self.mapFromGlobal(QtGui.QCursor.pos())
        inside = self.rect().contains(point)
        edge = max(5, round(10 * self.panel.chrome_scale))
        if inside and point.y() <= edge:
            self.reveal(self.top)
        elif inside and point.y() >= self.height() - edge:
            self.reveal(self.bottom)
        elif inside and any(
            overlay.isVisible() and overlay.geometry().contains(point) for overlay in (self.top, self.bottom)
        ):
            self.hide_timer.stop()
        elif self.top.isVisible() or self.bottom.isVisible():
            if not self.hide_timer.isActive():
                self.hide_timer.start()

    def hide_controls(self):
        if self.busy_controls():
            self.hide_timer.start()
            return
        focus = self.app.focusWidget()
        if focus is not None and any(overlay.isAncestorOf(focus) for overlay in (self.top, self.bottom)):
            self.panel.canvas.setFocus(QtCore.Qt.OtherFocusReason)
        self.top.hide()
        self.bottom.hide()

    def eventFilter(self, watched, event):
        if (
            event.type() == QtCore.QEvent.KeyPress
            and isinstance(event, QtGui.QKeyEvent)
            and event.key() == QtCore.Qt.Key_Escape
        ):
            # Escape also works when a revealed toolbar button owns focus.
            from .viewer_shortcuts import owner

            if owner(watched) is self.panel and self.app.activePopupWidget() is None:
                self.panel.close_fullscreen()
                event.accept()
                return True
        return False

    def restore(self):
        if self._restored:
            return
        self._restored = True
        self.timer.stop()
        self.hide_timer.stop()
        self.app.removeEventFilter(self)
        for index, widget, stretch in sorted(self.saved, key=lambda item: item[0]):
            hidden = widget.isHidden()
            if widget.parentWidget().layout():
                widget.parentWidget().layout().removeWidget(widget)
            self.panel.layout().insertWidget(index, widget, stretch)
            widget.setVisible(not hidden)

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)
