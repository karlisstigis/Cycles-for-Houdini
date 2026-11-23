# SPDX-License-Identifier: Apache-2.0
"""Real native panel fullscreen exits and live edge-overlay controls."""

import ctypes, json, os, time, traceback
from ctypes import wintypes
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
import hou
import cycles_houdini as ch
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices

dest = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
f = SimpleNamespace()
geo = hou.node("/obj").createNode("geo", "fullscreen_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
f.camera = hou.node("/obj").createNode("cam", "fullscreen_camera")
f.camera.parm("tz").set(5)
f.rop = hou.node("/out").createNode("cycles_ipr", "fullscreen_render")
set_devices(f.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
f.rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=f.camera.path(),
        resx=320,
        resy=180,
        ipr_resolution_percent=100,
        ipr_samples=4096,
        ipr_use_denoise=0,
        ipr_adaptive_sampling=0,
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        allowmotionblur=0,
        render_threads=2,
    )
)
tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
p = tab.activeInterfaceRootWidget()
floating = tab.floatingPanel()
native = hou.qt.floatingPanelWindow(floating)
p.set_rop(f.rop)
p.render()
report = dict(result="running", checks=[], ui_gaps=[], runtime=ch.__file__)


class CursorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


get_cursor = ctypes.windll.user32.GetCursorInfo
get_cursor.argtypes = [ctypes.POINTER(CursorInfo)]
load_cursor = ctypes.windll.user32.LoadCursorW
load_cursor.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
load_cursor.restype = wintypes.HANDLE
system_arrow = load_cursor(None, ctypes.c_void_p(32512))
assert system_arrow
report["uses_windows_system_arrow"] = True
report["native_cursor_samples"] = 0
started = since = time.monotonic()
phase = 0
busy = False
last = None
original = None
session = None
generation = None
window = None
canvas_rect = None
edit_generation = None
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(25)


def move(point):
    QtGui.QCursor.setPos(window.mapToGlobal(point))
    window.track_pointer()


def center():
    move(window.rect().center())


def ready():
    return (
        p.entry
        and not p.entry.error
        and p.canvas.full_size == (320, 180)
        and p.entry.applied_serial == p.entry.change_serial
        and not p._pending
        and not p._render_queued
    )


def stable():
    assert p.entry.session is session and p.entry.session.status()["generation"] == generation
    assert id(p.canvas) == canvas_id and len(registry.entries()) == 1


def record(text):
    report["checks"].append(text)


def finish(error=None):
    timer.stop()
    if p.background_button.dialog is not None:
        p.background_button.cancel_picker()
    p.dispose()
    floating.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error, wall_seconds=time.monotonic() - started)
    dest.write_text(json.dumps(report, indent=2))
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    global phase, since, busy, last, original, session, generation, window, canvas_rect, canvas_id, edit_generation
    if busy:
        return
    busy = True
    try:
        now = time.monotonic()
        assert now - started < 110, (phase, p.info.text())
        if last is not None:
            report["ui_gaps"].append(dict(phase=phase, seconds=now - last))
        last = now
        if p.entry:
            status = p.entry.session.status()
            assert not status["error"] and not p.entry.error, (status, p.entry.error)
        if phase == 0 and ready():
            session = p.entry.session
            generation = status["generation"]
            canvas_id = id(p.canvas)
            original = [p.layout().itemAt(i).widget() for i in range(p.layout().count())]
            native = hou.qt.floatingPanelWindow(floating) or p.window()
            # A native parent may hide its viewport cursor. The image canvas
            # must own its arrow, including after focus/navigation tools stop.
            p.setCursor(QtCore.Qt.BlankCursor)
            p.canvas.update_navigation_cursor()
            assert p.canvas.cursor().shape() == QtCore.Qt.ArrowCursor
            p.canvas.set_focus_enabled(True)
            assert p.canvas.cursor().shape() == QtCore.Qt.CrossCursor
            p.canvas._space_down = True
            p.canvas.update_navigation_cursor()
            assert p.canvas.cursor().shape() == QtCore.Qt.OpenHandCursor
            p.canvas._space_down = False
            p.canvas.set_focus_enabled(False)
            assert p.canvas.cursor().shape() == QtCore.Qt.ArrowCursor
            p.unsetCursor()
            assert p.canvas.testAttribute(QtCore.Qt.WA_OpaquePaintEvent)
            with (
                patch.object(hou.qt, "floatingPanelWindow", return_value=native),
                patch.object(native, "setMinimumWidth", wraps=native.setMinimumWidth) as minimum,
            ):
                p.monitor_scale._sync_minimum()
                minimum.reset_mock()
                for _ in range(12):
                    p.monitor_scale._sync_minimum()
                assert minimum.call_count == 0, "Unchanged native window minimum rewritten every poll"
            record("Canvas owns a visible cursor under a hidden parent, restores tool cursors, and paints opaquely")
            record("Stable native window constraints are not rewritten during monitor polling")
            floating.setIsFullscreen(True)
            since = now
            phase = 1
        elif phase == 1:
            if now - since > 0.4:
                QtGui.QCursor.setPos(
                    p.canvas.mapToGlobal(
                        p.canvas.rect().center() + QtCore.QPoint(round(60 * __import__("math").sin(now * 3)), 0)
                    )
                )
                ci = CursorInfo()
                ci.cbSize = ctypes.sizeof(ci)
                assert get_cursor(ctypes.byref(ci))
                assert ci.flags & 1, ("Windows cursor is hidden in native fullscreen", ci.flags)
                assert ci.hCursor == system_arrow, ("Cursor must be the Windows system arrow", ci.hCursor, system_arrow)
                report["native_cursor_samples"] += 1
            if now - since > 1.2:
                assert floating.isFullscreen() and p._fullscreen is None
                native = hou.qt.floatingPanelWindow(floating) or p.window()
                with patch.object(native, "setMinimumWidth", wraps=native.setMinimumWidth) as minimum:
                    for _ in range(12):
                        p.monitor_scale.update()
                    assert minimum.call_count == 0
                QtTest.QTest.keyClick(p.canvas, QtCore.Qt.Key_Escape)
                since = now
                phase = 2
        elif phase == 2 and now - since > 0.6:
            assert not floating.isFullscreen() and p._fullscreen is None
            stable()
            record("Escape exits real Houdini-owned fullscreen and stays out across monitor polls")
            record(
                "Windows reports a visible cursor during native fullscreen pointer motion; no geometry constraint writes"
            )
            floating.setIsFullscreen(True)
            since = now
            phase = 3
        elif phase == 3 and now - since > 0.4:
            assert floating.isFullscreen()
            QtTest.QTest.mouseClick(p.fullscreen_button, QtCore.Qt.LeftButton)
            since = now
            phase = 4
        elif phase == 4 and now - since > 0.6:
            assert not floating.isFullscreen() and p._fullscreen is None
            stable()
            record("Viewer fullscreen button exits native fullscreen without stacking a second window")
            floating.setIsFullscreen(True)
            since = now
            phase = 5
        elif phase == 5 and now - since > 0.3:
            floating.setIsFullscreen(False)
            since = now
            phase = 6
        elif phase == 6 and now - since > 0.6:
            assert not floating.isFullscreen()
            stable()
            record("Houdini native menu/API exit remains stable")
            p.open_fullscreen()
            window = p._fullscreen
            center()
            since = now
            phase = 7
        elif phase == 7 and now - since > 0.6:
            assert window.isFullScreen() and p.canvas.geometry() == window.rect()
            assert not window.top.isVisible() and not window.bottom.isVisible()
            stable()
            p.canvas.set_zoom(100)
            p.resolution_link.setChecked(True)
            canvas_rect = QtCore.QRect(p.canvas.geometry())
            move(QtCore.QPoint(window.width() // 2, 2))
            since = now
            phase = 8
        elif phase == 8 and now - since > 0.15:
            assert window.top.isVisible() and not window.bottom.isVisible()
            assert p.canvas.geometry() == canvas_rect
            stable()
            QtTest.QTest.mouseClick(p.checker, QtCore.Qt.LeftButton)
            center()
            since = now
            phase = 9
        elif phase == 9 and now - since > 0.15:
            assert window.top.isVisible(), "Chrome vanished before the hide delay"
            phase = 10
        elif phase == 10 and now - since > 0.75:
            assert not window.top.isVisible()
            stable()
            record(
                "Top edge controls hide after 450 ms without resizing/restarting, including with resolution/zoom linked"
            )
            move(QtCore.QPoint(window.width() // 2, window.height() - 2))
            since = now
            phase = 11
        elif phase == 11 and now - since > 0.15:
            assert window.bottom.isVisible() and p.canvas.geometry() == canvas_rect
            p.display.showPopup()
            center()
            since = now
            phase = 12
        elif phase == 12 and now - since > 0.8:
            assert QtWidgets.QApplication.activePopupWidget() is not None and window.bottom.isVisible()
            p.display.hidePopup()
            center()
            since = now
            phase = 13
        elif phase == 13 and now - since > 0.8:
            assert not window.bottom.isVisible()
            stable()
            record("Bottom controls and open dropdowns work; hiding waits for popup dismissal")
            move(QtCore.QPoint(window.width() // 2, 2))
            p.background_button.choose()
            since = now
            phase = 14
        elif phase == 14 and now - since > 0.8:
            assert window.top.isVisible() and p.background_button.dialog.parentWidget() is window
            p.background_button.cancel_picker()
            center()
            since = now
            phase = 15
        elif phase == 15 and now - since > 0.8:
            assert not window.top.isVisible()
            stable()
            record("Color picker stays above fullscreen and holds chrome until closed")
            f.camera.parm("tx").set(0.2)
            since = now
            phase = 16
        elif phase == 16 and ready() and status["generation"] > generation:
            generation = status["generation"]
            assert p.canvas.window() is window
            record("Camera edits keep updating the same live renderer in fullscreen")
            move(QtCore.QPoint(window.width() // 2, 2))
            since = now
            phase = 17
        elif phase == 17 and now - since > 0.2:
            window.grab().save(str(dest.with_suffix(".top.png")))
            QtTest.QTest.mouseClick(p.fullscreen_button, QtCore.Qt.LeftButton)
            since = now
            phase = 18
        elif phase == 18 and now - since > 0.3:
            assert p._fullscreen is None and p.canvas.parentWidget() is p
            assert original == [p.layout().itemAt(i).widget() for i in range(p.layout().count())]
            stable()
            record("Revealed fullscreen toggle restores every original widget/layout slot")
            p.open_fullscreen()
            window = p._fullscreen
            center()
            since = now
            phase = 19
        elif phase == 19 and now - since > 0.3:
            QtTest.QTest.keyClick(p.canvas, QtCore.Qt.Key_Escape)
            since = now
            phase = 20
        elif phase == 20 and now - since > 0.3:
            assert p._fullscreen is None and p.canvas.parentWidget() is p
            stable()
            assert original == [p.layout().itemAt(i).widget() for i in range(p.layout().count())]
            record("Repeated fullscreen/Escape cycles restore controls and leave one renderer")
            p.open_fullscreen()
            window = p._fullscreen
            p.dispose()
            assert p._fullscreen is None and window._restored and not window.timer.isActive()
            record("Panel disposal restores widgets and stops fullscreen timers/event filters")
            finish()
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
