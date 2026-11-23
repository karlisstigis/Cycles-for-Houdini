# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: the floating Cycles Render View works again after close and reopen.

The shelf IPR window (rop/render_actions.open_ipr) renders a small scene, is
closed and opened again. Each time the toolbar and the image canvas must be
visible with sensible geometry, the viewer's native window (if it has one)
must be shown and cover the window, the screen over the window must show the viewer (not an
unpainted white or black client area) and a rendered image must arrive. The
window is raised above other windows for a moment for the screen grab, which
is kept only when every sampled point belongs to it (never another
application's pixels). Qt's grab and the screen grab are saved beside the GUI
report.

Busy retirement: the reopened window renders a dense mesh (2.25 million
quads) and is closed while the worker builds it. The worker is suspended when
the shutdown command reaches it, so it cannot answer within the controller's
5 s grace and the controller stops it itself (as for an engine update that
cannot be interrupted). That forced stop must not be reported as a renderer
failure: no exception from the retirement poll, the session ends "closed",
leaves the retiring list, and a new window renders again. Writes only the
GUI report and the screenshots; never saves the scene.
"""

import json
import os
import sys
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import shiboken6
from PySide6 import QtCore, QtGui
import cycles_houdini as ch
from cycles_houdini import device_selection
from cycles_houdini.ipr import queued, registry
from cycles_houdini.rop import render_actions

REPORT = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
SHOTS = REPORT.parent
DEVICE = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
report = dict(result="running", device=DEVICE, package=str(Path(ch.__file__).parent), checks={})
failures = []
# The harness names a backend; a GPU backend needs the exact device id.
DEVICE_IDS = (
    [DEVICE] if DEVICE == "CPU" else [d["id"] for d in device_selection.devices() if d.get("backend") == DEVICE][:1]
)
assert DEVICE_IDS, "No render device for " + DEVICE


def save():
    report["failures"] = failures
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def emission_rop(name, geo_path, cam_path, resolution=(320, 180)):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(
            vobject=geo_path,
            camera=cam_path,
            resx=resolution[0],
            resy=resolution[1],
            samples=4,
            ipr_samples=4,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
        )
    )
    device_selection.set_devices(rop, DEVICE_IDS)
    return rop


mat = ch.create_material(name="reopen_emission", shader="emission")
mat.node("surface_shader").setParms(dict(strength=4.0))
mat.node("surface_shader").parmTuple("color").set((0.9, 0.35, 0.1))
small = hou.node("/obj").createNode("geo", "reopen_small", run_init_scripts=False)
small.createNode("sphere").setParms({"type": 2})
small.parm("shop_materialpath").set(mat.path())
cam = hou.node("/obj").createNode("cam", "reopen_camera")
cam.setParms(dict(tz=5, projection=1, orthowidth=4))
small_rop = emission_rop("reopen_small", small.path(), cam.path())
dense = hou.node("/obj").createNode("geo", "reopen_dense", run_init_scripts=False)
grid = dense.createNode("grid")
grid.setParms(dict(rows=1500, cols=1500))
grid.parmTuple("size").set((3, 3))
dense.parm("shop_materialpath").set(mat.path())
dense.parm("rx").set(90)
dense_rop = emission_rop("reopen_dense", dense.path(), cam.path())


def rgb(image, step=2):
    image = image.convertToFormat(QtGui.QImage.Format_RGB32)
    data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return data[:, : image.width() * 4].reshape(image.height(), image.width(), 4)[::step, ::step, :3]


def win32():
    """Private library handles: declaring prototypes must not change Houdini's shared ctypes.windll."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    for function, result, arguments in (
        (user32.GetClientRect, wintypes.BOOL, (wintypes.HWND, ctypes.POINTER(wintypes.RECT))),
        (user32.GetWindowRect, wintypes.BOOL, (wintypes.HWND, ctypes.POINTER(wintypes.RECT))),
        (user32.IsWindowVisible, wintypes.BOOL, (wintypes.HWND,)),
        (user32.GetAncestor, wintypes.HWND, (wintypes.HWND, wintypes.UINT)),
        (user32.WindowFromPoint, wintypes.HWND, (wintypes.POINT,)),
        (user32.MapWindowPoints, ctypes.c_int, (wintypes.HWND, wintypes.HWND, ctypes.c_void_p, wintypes.UINT)),
        (
            user32.SetWindowPos,
            wintypes.BOOL,
            (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT),
        ),
    ):
        function.restype, function.argtypes = result, arguments
    return ctypes, wintypes, user32


def native_state(window, panel):
    """The viewer's native window (its root, as Houdini's RE_Window) inside the top-level window."""
    ctypes, wintypes, user32 = win32()
    top = wintypes.HWND(int(window.winId()))
    client = wintypes.RECT()
    user32.GetClientRect(top, ctypes.byref(client))
    state = dict(top_client=[client.right, client.bottom], top_visible=bool(user32.IsWindowVisible(top)))
    if not panel.internalWinId():
        return dict(state, panel_native=False)
    child = wintypes.HWND(int(panel.internalWinId()))
    rect = wintypes.RECT()
    user32.GetWindowRect(child, ctypes.byref(rect))
    user32.MapWindowPoints(None, top, ctypes.pointer(rect), 2)
    handle = panel.windowHandle()
    parent = user32.GetAncestor(child, 1)  # GA_PARENT
    return dict(
        state,
        panel_native=True,
        panel_visible=bool(user32.IsWindowVisible(child)),
        panel_rect=[rect.left, rect.top, rect.right, rect.bottom],
        panel_parent_is_window=bool(parent) and int(parent) == int(window.winId()),
        panel_qwindow_visible=handle.isVisible() if handle is not None else None,
        panel_qwindow_exposed=handle.isExposed() if handle is not None else None,
    )


def screen_capture(window):
    """The screen over the window's client area, raised above other windows for a moment.

    Win32 coordinates throughout (ClientToScreen, WindowFromPoint, BitBlt from
    the screen), so no logical/physical pixel conversion is involved. Returns
    None unless every sampled point belongs to this window, so the grab never
    holds another application's pixels.
    """
    ctypes, wintypes, user32 = win32()
    gdi32 = ctypes.WinDLL("gdi32")
    for function, result, arguments in (
        (user32.ClientToScreen, wintypes.BOOL, (wintypes.HWND, ctypes.POINTER(wintypes.POINT))),
        (user32.GetDC, wintypes.HDC, (wintypes.HWND,)),
        (user32.ReleaseDC, ctypes.c_int, (wintypes.HWND, wintypes.HDC)),
        (gdi32.CreateCompatibleDC, wintypes.HDC, (wintypes.HDC,)),
        (gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, (wintypes.HDC, ctypes.c_int, ctypes.c_int)),
        (gdi32.SelectObject, wintypes.HGDIOBJ, (wintypes.HDC, wintypes.HGDIOBJ)),
        (gdi32.DeleteObject, wintypes.BOOL, (wintypes.HGDIOBJ,)),
        (gdi32.DeleteDC, wintypes.BOOL, (wintypes.HDC,)),
        (
            gdi32.BitBlt,
            wintypes.BOOL,
            (wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC)
            + (ctypes.c_int, ctypes.c_int, wintypes.DWORD),
        ),
        (
            gdi32.GetDIBits,
            ctypes.c_int,
            (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p)
            + (wintypes.UINT,),
        ),
    ):
        function.restype, function.argtypes = result, arguments
    top = wintypes.HWND(int(window.winId()))
    flags = 0x0001 | 0x0002 | 0x0010  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    user32.SetWindowPos(top, wintypes.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
    try:
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            QtCore.QCoreApplication.processEvents()
            time.sleep(0.02)
        client = wintypes.RECT()
        user32.GetClientRect(top, ctypes.byref(client))
        width, height = client.right, client.bottom
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(top, ctypes.byref(origin))
        for fx in (0.02, 0.5, 0.98):
            for fy in (0.02, 0.5, 0.98):
                owner = user32.WindowFromPoint(
                    wintypes.POINT(origin.x + round(width * fx), origin.y + round(height * fy))
                )
                root = user32.GetAncestor(owner, 2) if owner else None  # GA_ROOT
                if not root or int(root) != int(window.winId()):
                    return None
        screen_dc = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            copied = gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, origin.x, origin.y, 0x00CC0020)
            header = (ctypes.c_uint32 * 10)(40, width, (-height) & 0xFFFFFFFF, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            lines = gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, header, 0)
        finally:
            gdi32.SelectObject(memory_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)
        assert copied and lines == height, (copied, lines, width, height)
        image = QtGui.QImage(buffer.raw, width, height, width * 4, QtGui.QImage.Format_RGB32).copy()
        return image.scaled(window.width(), window.height(), QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.FastTransformation)
    finally:
        user32.SetWindowPos(top, wintypes.HWND(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST


def widget_state(widget, window):
    top_left = widget.mapTo(window, QtCore.QPoint(0, 0))
    return dict(
        visible=widget.isVisible(),
        size=[widget.width(), widget.height()],
        position=[top_left.x(), top_left.y()],
        native=bool(widget.internalWinId()),
    )


def check_window(window, label):
    """Toolbar and canvas are shown, laid out inside the window, and drawn."""
    panel = window.panel
    canvas = panel.canvas
    state = dict(
        window=[window.width(), window.height()],
        window_visible=window.isVisible(),
        panel=widget_state(panel, window),
        render_button=widget_state(panel.render_button, window),
        pass_menu=widget_state(panel.pass_menu, window),
        canvas=widget_state(canvas, window),
        display_mode=getattr(canvas, "display_mode", ""),
        gl_failure=getattr(canvas, "gl_failure", ""),
        image=[canvas.image.width(), canvas.image.height()],
        native=native_state(window, panel),
        screen=dict(
            name=window.screen().name(),
            ratio=window.devicePixelRatioF(),
            frame=list(window.frameGeometry().getRect()),
            client_origin=[window.mapToGlobal(QtCore.QPoint(0, 0)).x(), window.mapToGlobal(QtCore.QPoint(0, 0)).y()],
        ),
    )
    window.grab().save(str(SHOTS / f"{label}-qt-grab.png"))
    seen = screen_capture(window)
    state["screen_captured"] = seen is not None
    if seen is not None:
        seen.save(str(SHOTS / f"{label}-screen.png"))
        pixels = rgb(seen).astype(np.int16)
        state["screen_white"] = round(float((pixels.min(axis=2) >= 250).mean()), 3)
        state["screen_black"] = round(float((pixels.max(axis=2) <= 5).mean()), 3)
        # The middle of the canvas against the classic QPainter drawing of the same view.
        inner = canvas.rect().adjusted(
            canvas.width() * 3 // 8, canvas.height() * 3 // 8, -canvas.width() * 3 // 8, -canvas.height() * 3 // 8
        )
        reference = QtGui.QImage(canvas.size(), QtGui.QImage.Format_RGB32)
        reference.fill(QtGui.QColor(0, 0, 0))
        painter = QtGui.QPainter(reference)
        canvas.paint_scene(painter)
        painter.end()
        middle = rgb(seen.copy(inner.translated(canvas.mapTo(window, QtCore.QPoint(0, 0))))).astype(np.int16)
        wanted = rgb(reference.copy(inner)).astype(np.int16)
        rows, columns = min(middle.shape[0], wanted.shape[0]), min(middle.shape[1], wanted.shape[1])
        state["canvas_difference"] = round(float(np.abs(middle[:rows, :columns] - wanted[:rows, :columns]).mean()), 2)
    report["checks"][label] = state
    save()
    problems = []
    for name in ("panel", "render_button", "pass_menu", "canvas"):
        part = state[name]
        if not part["visible"] or min(part["size"]) <= 0:
            problems.append(f"{name} not shown: {part}")
        elif part["position"][0] < 0 or part["position"][1] < 0:
            problems.append(f"{name} outside the window: {part}")
    if state["canvas"]["size"][0] < window.width() // 2 or state["canvas"]["size"][1] < window.height() // 3:
        problems.append(f"canvas too small: {state['canvas']}")
    native = state["native"]
    if native.get("panel_native"):
        if not native["panel_visible"] or not native["panel_qwindow_exposed"]:
            problems.append(f"viewer's native window not shown: {native}")
        elif not native["panel_parent_is_window"]:
            problems.append(f"viewer's native window not inside the window: {native}")
        else:
            left, top, right, bottom = native["panel_rect"]
            width, height = native["top_client"]
            if left > 2 or top > 2 or right < width - 2 or bottom < height - 2:
                problems.append(f"viewer's native window does not cover the window: {native}")
    if seen is not None:
        if state["screen_white"] > 0.3:
            problems.append(f"window mostly unpainted white on screen ({state['screen_white']})")
        if state["canvas_difference"] > 40:
            problems.append(f"screen over the canvas differs from the drawing ({state['canvas_difference']})")
    if problems:
        failures.append((label, problems))


def wait(predicate, what, timeout=45.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for " + what)
        yield


def pause(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        yield


def rendered(window):
    panel = window.panel
    entry = panel.entry
    if entry is None:
        return False
    status = entry.session.status()
    assert status["state"] != "failed", status
    return status["state"] == "complete" and not panel.canvas.image.isNull()


def open_and_check(label):
    window = render_actions.open_ipr(small_rop)
    yield from wait(lambda: rendered(window), label + " image")
    yield from pause(0.5)
    check_window(window, label)
    return window


def busy_retirement(window):
    """Close the window while its worker builds a dense scene; the forced stop is not a failure."""
    panel = window.panel
    panel.set_rop(dense_rop)
    panel.render()
    yield from wait(lambda: panel.entry is not None and panel.entry.rop == dense_rop, "dense session")
    session = panel.entry.session
    controller = session._controller
    # The worker has accepted the scene and is building it (the apply reply came back).
    yield from wait(lambda: controller.backend is not None and controller.events, "dense scene accepted", 90.0)
    status = session.status()
    state = dict(status_at_close={key: status.get(key) for key in ("state", "message", "renderer_pid")})
    kills, errors, suspended = [], [], []
    backend = controller.backend
    process = backend.process

    def busy_close():
        # The worker receives the shutdown command while it cannot answer it, as
        # during an engine update that cannot be interrupted (on CPU, Cycles
        # cancels its BVH builds within a fraction of a second, so a real scene
        # rarely outlasts the 5 s grace in a test).
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtSuspendProcess.restype = ctypes.c_long
        ntdll.NtSuspendProcess.argtypes = (wintypes.HANDLE,)
        suspended.append(ntdll.NtSuspendProcess(wintypes.HANDLE(int(process._handle))))
        return type(backend).close(backend)

    backend.close = busy_close
    original_kill = process.kill

    def recorded_kill():
        kills.append(time.monotonic())
        return original_kill()

    process.kill = recorded_kill
    original_poll = queued._poll_retirements

    def recorded_poll():
        try:
            return original_poll()
        except BaseException:
            errors.append(traceback.format_exc())
            raise

    queued._poll_retirements = recorded_poll
    try:
        began = time.monotonic()
        window.close()
        yield from wait(lambda: session in queued._retiring or session._capture_closed, "retirement start", 10.0)
        yield from wait(lambda: session not in queued._retiring, "retirement end", 30.0)
        yield from pause(0.3)
    finally:
        queued._poll_retirements = original_poll
    final = session.status()
    state.update(
        seconds=round(time.monotonic() - began, 3),
        suspended=suspended,
        worker_stopped_by_controller=bool(kills),
        errors=errors,
        final_state=final["state"],
        final_error=final.get("error", ""),
        retirement_done=getattr(session, "_retirement_done", None),
        registry_entry=registry._entries.get(f"rop:{dense_rop.sessionId()}") is not None,
    )
    report["checks"]["busy_retirement"] = state
    save()
    assert suspended == [0], suspended
    assert kills, "The worker stopped within the grace period; the forced stop was not exercised"
    assert not errors, errors
    assert final["state"] == "closed" and not final.get("error"), final
    assert state["retirement_done"] and not state["registry_entry"], state


def tear_off_api():
    names = sorted(
        {
            f"{cls.__name__}.{name}"
            for cls in (hou.PaneTab, hou.Pane, hou.FloatingPanel, hou.Desktop)
            for name in dir(cls)
            if any(word in name.lower() for word in ("float", "tear", "detach", "attach", "split"))
        }
    )
    report["tear_off_api"] = names


def workflow():
    tear_off_api()
    first = yield from open_and_check("first")
    report["first_window"] = hex(id(first))
    first.close()
    yield from wait(lambda: render_actions._ipr_window is None, "first window destroyed", 10.0)
    yield from pause(0.5)
    second = yield from open_and_check("reopened")
    assert second is not first
    try:
        yield from busy_retirement(second)
    except AssertionError as error:
        failures.append(("busy_retirement", str(error)))
        if shiboken6.isValid(second):
            second.close()
    yield from wait(lambda: render_actions._ipr_window is None, "second window destroyed", 10.0)
    third = yield from open_and_check("after_retirement")
    third.close()
    # Every check runs (one run shows all symptoms); any failure fails the test.
    assert not failures, failures


steps = workflow()
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(20)


def finish(error=None):
    timer.stop()
    try:
        if render_actions._ipr_window is not None:
            render_actions._ipr_window.close()
        registry.close_all()
    except Exception:
        error = (error or "") + "\ncleanup: " + traceback.format_exc()
    report.update(result="failed" if error else "passed", error=error)
    save()
    print("REOPEN_REPORT " + json.dumps(dict(result=report["result"], error=error)), file=sys.stderr, flush=True)
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def tick():
    try:
        next(steps)
    except StopIteration:
        finish()
    except BaseException:
        finish(traceback.format_exc())


save()
timer.timeout.connect(tick)
timer.start()
