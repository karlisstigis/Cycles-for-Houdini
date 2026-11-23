# SPDX-License-Identifier: Apache-2.0
"""Viewer-scoped shortcuts, independent of image-widget mouse focus."""

from PySide6 import QtCore, QtGui, QtWidgets


def owner(widget):
    while isinstance(widget, QtWidgets.QWidget):
        value = getattr(widget, "_cycles_shortcut_owner", None)
        if value is not None:
            return value
        widget = widget.parentWidget()
    return None


def editing(widget):
    while isinstance(widget, QtWidgets.QWidget):
        if isinstance(
            widget, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit, QtWidgets.QAbstractSpinBox)
        ):
            return True
        if isinstance(widget, QtWidgets.QComboBox) and widget.isEditable():
            return True
        widget = widget.parentWidget()
    return False


class ViewerShortcuts(QtCore.QObject):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.space = False
        panel._cycles_shortcut_owner = panel
        panel.canvas._cycles_shortcut_owner = panel  # Also when reparented for fullscreen.
        self.app = QtWidgets.QApplication.instance()
        self.app.installEventFilter(self)
        self.enabled = True

    def set_enabled(self, enabled):
        """A hidden viewer takes no application-wide key events."""
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if enabled:
            self.app.installEventFilter(self)
        else:
            self.close()

    def close(self):
        self.enabled = False
        self.app.removeEventFilter(self)
        self.panel.canvas._space_down = False
        self.space = False

    def eventFilter(self, watched, event):
        p = self.panel
        if p._disposed:
            return False
        kind = event.type()
        if kind in (QtCore.QEvent.FocusOut, QtCore.QEvent.Hide, QtCore.QEvent.WindowDeactivate) and watched in (
            p,
            p.canvas,
        ):
            self.space = False
            p.canvas._space_down = False
            return False
        if kind == QtCore.QEvent.KeyRelease:
            # This filter also sees native Scene View navigation events. Do not
            # inspect their key payload unless releasing our own Space gesture.
            if not self.space:
                return False
            if owner(watched) is not p:
                self.space = False
                p.canvas._space_down = False
                p.canvas.update_navigation_cursor()
                return False
            if not isinstance(event, QtGui.QKeyEvent) or event.key() != QtCore.Qt.Key_Space:
                return False
            if not event.isAutoRepeat():
                self.space = False
                p.canvas.keyReleaseEvent(event)
            event.accept()
            return True
        if kind not in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress):
            return False
        if not p.canvas.isVisible() or self.app.activePopupWidget() is not None:
            return False
        if self.app.activeWindow() not in (p.window(), p.canvas.window()):
            return False
        focus = self.app.focusWidget()
        if editing(focus) or editing(watched):
            return False
        hovered = owner(self.app.widgetAt(QtGui.QCursor.pos()))
        target = hovered or owner(focus) or owner(watched)
        if target is not p:
            return False
        if not isinstance(event, QtGui.QKeyEvent):
            return False
        key, modifiers = event.key(), event.modifiers()
        channels = {QtCore.Qt.Key_R: "r", QtCore.Qt.Key_G: "g", QtCore.Qt.Key_B: "b", QtCore.Qt.Key_A: "alpha"}
        navigation = (QtCore.Qt.Key_F1, QtCore.Qt.Key_F, QtCore.Qt.Key_Home, QtCore.Qt.Key_Space, QtCore.Qt.Key_Escape)
        if modifiers != QtCore.Qt.NoModifier and not (key == QtCore.Qt.Key_F and modifiers == QtCore.Qt.ShiftModifier):
            return False
        if key not in channels and key != QtCore.Qt.Key_T and key not in navigation:
            return False
        # Claim before Houdini or a focused toolbar button consumes the key.
        if kind == QtCore.QEvent.ShortcutOverride:
            event.accept()
            return True
        if not event.isAutoRepeat():
            if key in channels:
                p.toggle_channel(channels[key])
            elif key == QtCore.Qt.Key_T:
                p.checker.click()
            elif key == QtCore.Qt.Key_F1:
                p.help_button.click()
            else:
                p.canvas.setFocus(QtCore.Qt.ShortcutFocusReason)
                if key == QtCore.Qt.Key_Space:
                    self.space = True
                p.canvas.keyPressEvent(event)
        event.accept()
        return True
