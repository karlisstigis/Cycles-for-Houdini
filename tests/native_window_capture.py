# SPDX-License-Identifier: Apache-2.0
"""Capture this process's named native Houdini floating panel from the desktop."""

import ctypes
from ctypes import wintypes
import os

from PySide6 import QtGui


class WindowOccluded(RuntimeError):
    pass


def native_window(viewer):
    panel = viewer.floatingPanel()
    name = panel.name()
    windows = []
    api = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, unused):
        pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            title = ctypes.create_unicode_buffer(1024)
            api.GetWindowTextW(hwnd, title, 1024)
            if name in title.value:
                rect = wintypes.RECT()
                api.GetWindowRect(hwnd, ctypes.byref(rect))
                windows.append((int(hwnd), title.value, [rect.left, rect.top, rect.right, rect.bottom]))
        return True

    api.EnumWindows(visit, 0)
    assert len(windows) == 1, (name, windows)
    return windows[0]


def show(viewer):
    """Map only the uniquely named pane owned by this isolated test process."""
    hwnd, title, bounds = native_window(viewer)
    api = ctypes.windll.user32
    api.ShowWindow(hwnd, 5)  # SW_SHOW; the GUI launcher starts Houdini hidden.
    api.BringWindowToTop(hwnd)
    api.SetForegroundWindow(hwnd)
    return dict(hwnd=hwnd, title=title, bounds=bounds, visible=bool(api.IsWindowVisible(hwnd)))


def capture(viewer):
    hwnd, title, bounds = native_window(viewer)
    api = ctypes.windll.user32
    api.WindowFromPoint.argtypes = [wintypes.POINT]
    api.WindowFromPoint.restype = wintypes.HWND
    api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    api.GetAncestor.restype = wintypes.HWND
    x, y, right, bottom = bounds
    for fx in (0.25, 0.5, 0.75):
        for fy in (0.35, 0.55, 0.75):
            point = wintypes.POINT(int(x + (right - x) * fx), int(y + (bottom - y) * fy))
            under = api.WindowFromPoint(point)
            root = int(api.GetAncestor(under, 2) or 0)  # GA_ROOT
            if root != hwnd:
                raise WindowOccluded(f"Owned native pane {hwnd} is obscured at {(point.x, point.y)} by root {root}")
    screen = QtGui.QGuiApplication.primaryScreen()
    desktop = screen.grabWindow(0)
    ratio = desktop.devicePixelRatio()
    geometry = screen.geometry()
    # The Win32 rectangle and screenshot image use physical screen pixels in
    # Houdini's DPI-aware process; QScreen.geometry uses logical coordinates.
    image = desktop.toImage().copy(int(x - geometry.x() * ratio), int(y - geometry.y() * ratio), right - x, bottom - y)
    # Native Render View does not necessarily have a QWidget parent. Do not
    # substitute the main Qt window and accidentally capture the wrong pane.
    widget = viewer.qtParentWindow()
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
    foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    return (
        image,
        widget.grab().toImage() if widget else None,
        dict(
            hwnd=hwnd,
            title=title,
            bounds=bounds,
            screen=screen.name(),
            device_pixel_ratio=ratio,
            foreground_hwnd=foreground,
            visible=bool(ctypes.windll.user32.IsWindowVisible(hwnd)),
        ),
    )
