# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: image-only fullscreen shows the image at once and never a blank frame.

For gpu, native and classic display modes (-RunName ..__display_<mode> picks
one), a docked viewer renders a small scene. With the pointer parked in the
middle of the viewer (no edge toolbar revealed), it enters image-only
fullscreen with a completed render and again with a refining render. The
screen itself is grabbed (QScreen.grabWindow over the middle of the canvas,
away from the edge toolbars) repeatedly and compared with the classic QPainter
drawing of the same view (CanvasBehaviour.paint_scene): a frame is blank
when the screen is black where the drawing is not, or when it differs from it
grossly. It counts blank frames while refining, after revealing and hiding
each edge toolbar, and right after returning to the docked panel, and records
when the image first appears. Writes the GUI report and one FULLSCREEN_REPORT
stderr line; never saves.
"""

import json
import os
from pathlib import Path
import sys
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from cycles_houdini.ipr import registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.viewer.viewer_display import ENVIRONMENT as DISPLAY_ENVIRONMENT

MODES = [os.environ[DISPLAY_ENVIRONMENT]] if os.environ.get(DISPLAY_ENVIRONMENT) else ["gpu", "native", "classic"]
# Also a floating gpu viewer on the last screen (the HD monitor here).
LAYOUTS = [(mode, "docked") for mode in MODES] + ([("gpu", "floating")] if "gpu" in MODES else [])
FIRST_IMAGE_SECONDS = 0.5


def pixels(image, step=3):
    image = image.convertToFormat(QtGui.QImage.Format_RGB32)
    data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    data = data[:, : image.width() * 4].reshape(image.height(), image.width(), 4)[::step, ::step, :3]
    return data.astype(np.int16)


def sleep(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        yield


def wait_until(predicate, what, timeout=40.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for " + what)
        yield


def fullscreen_border(window):
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    handle = ctypes.c_void_p(int(window.winId()))
    rect = Rect()
    user32.GetWindowRect(handle, ctypes.byref(rect))
    native = [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]
    screens = [
        [s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height()]
        for s in QtWidgets.QApplication.screens()
    ]
    return dict(
        ws_border=bool(user32.GetWindowLongPtrW(handle, -16) & 0x00800000),
        native_rect=native,
        covers_screen_exactly=native in screens,
        opengl=type(window.panel.canvas).__name__ == "GpuImageCanvas",
    )


def progress(*parts):
    sys.__stderr__.write("FULLSCREEN " + " ".join(str(p) for p in parts) + "\n")
    sys.__stderr__.flush()


class _Filter(QtCore.QObject):
    def __init__(self, probe):
        super().__init__()
        self.probe = probe

    def eventFilter(self, watched, event):
        return self.probe.eventFilter(watched, event)


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
        self.report = dict(result="running", failures=[], notes=[], modes={})
        self.desktop = hou.ui.curDesktop()
        geo = hou.node("/obj").createNode("geo", "fullscreen_subject", run_init_scripts=False)
        sphere = geo.createNode("sphere")
        sphere.parm("type").set("polymesh")
        sphere.setDisplayFlag(True)
        sphere.setRenderFlag(True)
        light = hou.node("/obj").createNode("hlight::2.0", "fullscreen_light")
        light.parmTuple("t").set((2, 3, 4))
        camera = hou.node("/obj").createNode("cam", "fullscreen_camera")
        camera.parmTuple("t").set((0.3, 0.2, 4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "fullscreen_render")
        set_devices(self.rop, self.device)
        self.rop.setParms(
            dict(
                camera=camera.path(),
                resx=480,
                resy=270,
                ipr_samples=8,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
            )
        )
        self.tab = None
        self.expose_times = None
        self.filter = _Filter(self)
        self.started = time.monotonic()
        self.finished = False
        self.work = self.run()
        self.driver = QtCore.QTimer()
        self.driver.setInterval(16)
        self.driver.timeout.connect(self.on_drive)
        self.driver.start()

    def fail(self, mode, message, detail=None):
        self.report["failures"].append(dict(mode=mode, problem=message, detail=detail))

    def on_drive(self):
        if self.finished:
            return
        try:
            if time.monotonic() - self.started > 400:
                raise TimeoutError("Probe exceeded 400 s")
            next(self.work)
        except StopIteration:
            self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def sample(self, panel, label):
        """Grab the screen over the middle of the canvas and compare it with the classic drawing."""
        canvas = panel.canvas
        # The middle of the canvas (inside the fitted image), away from the edge toolbars.
        inner = canvas.rect().adjusted(
            canvas.width() * 3 // 8, canvas.height() * 3 // 8, -canvas.width() * 3 // 8, -canvas.height() * 3 // 8
        )
        reference = QtGui.QImage(canvas.size(), QtGui.QImage.Format_RGB32)
        reference.fill(QtGui.QColor(0, 0, 0))
        painter = QtGui.QPainter(reference)
        canvas.paint_scene(painter)
        painter.end()
        origin = canvas.mapToGlobal(inner.topLeft())
        screen = (
            QtWidgets.QApplication.screenAt(origin + QtCore.QPoint(inner.width() // 2, inner.height() // 2))
            or canvas.screen()
        )
        geometry = screen.geometry()
        shot = screen.grabWindow(0, origin.x() - geometry.x(), origin.y() - geometry.y(), inner.width(), inner.height())
        seen = pixels(shot.toImage())
        wanted = pixels(reference.copy(inner))
        rows, columns = min(seen.shape[0], wanted.shape[0]), min(seen.shape[1], wanted.shape[1])
        seen, wanted = seen[:rows, :columns], wanted[:rows, :columns]
        black_seen = float((seen.max(axis=2) < 10).mean())
        black_wanted = float((wanted.max(axis=2) < 10).mean())
        difference = float(np.abs(seen - wanted).mean())
        blank = (black_seen > 0.9 and black_wanted < 0.5) or difference > 40
        return dict(
            label=label,
            t=round(time.monotonic() - self.phase_started, 3),
            black_screen=round(black_seen, 3),
            black_drawing=round(black_wanted, 3),
            mean_difference=round(difference, 2),
            equal_fraction=round(float((np.abs(seen - wanted).max(axis=2) == 0).mean()), 4),
            blank=blank,
            # Where it differs: mean |difference| and screen blackness on a 4 x 4 grid.
            grid=[
                [
                    [
                        round(
                            float(
                                np.abs(
                                    seen[i * rows // 4 : (i + 1) * rows // 4, j * columns // 4 : (j + 1) * columns // 4]
                                    - wanted[
                                        i * rows // 4 : (i + 1) * rows // 4, j * columns // 4 : (j + 1) * columns // 4
                                    ]
                                ).mean()
                            ),
                            1,
                        ),
                        round(
                            float(
                                (
                                    seen[
                                        i * rows // 4 : (i + 1) * rows // 4, j * columns // 4 : (j + 1) * columns // 4
                                    ].max(axis=2)
                                    < 10
                                ).mean()
                            ),
                            2,
                        ),
                    ]
                    for j in range(4)
                ]
                for i in range(4)
            ]
            if blank or difference > 1
            else None,
            region=[origin.x(), origin.y(), inner.width(), inner.height()],
            paints=self.paints,
            window=type(canvas.window()).__name__,
        )

    def series(self, panel, label, seconds, interval=0.1):
        samples = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            samples.append(self.sample(panel, label))
            yield from sleep(interval)
        return samples

    def count_paint(self, *_):
        self.paints += 1
        if getattr(self, "paint_times", None) is not None:
            self.paint_times.append(round(time.monotonic() - self.phase_started, 3))

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Expose and self.expose_times is not None:
            self.expose_times.append((round(time.monotonic() - self.phase_started, 3), watched.isExposed()))
        return False

    def enter(self, panel, how):
        if how == "button click":
            # As the artist does: the pointer clicks the toolbar's Fullscreen button.
            from PySide6 import QtTest

            button = panel.fullscreen_button
            QtGui.QCursor.setPos(button.mapToGlobal(button.rect().center()))
            QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
        else:
            panel.open_fullscreen()

    def run_mode(self, mode, layout):
        record = self.report["modes"].setdefault(mode + ("" if layout == "docked" else " " + layout), {})
        os.environ[DISPLAY_ENVIRONMENT] = mode
        if layout == "docked":
            viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
            self.tab = viewer.pane().splitHorizontally().tabs()[0].setType(hou.paneTabType.PythonPanel)
        else:
            self.tab = self.desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 700))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        progress(mode, layout, "mount")
        if layout == "floating":
            yield from sleep(0.8)
            screens = QtWidgets.QApplication.screens()
            rect = screens[-1].availableGeometry()
            bottom = max(screen.geometry().bottom() + 1 for screen in screens)
            floating = self.tab.floatingPanel()
            floating.setSize((min(1100, rect.width() - 80), min(700, rect.height() - 80)))
            floating.setPosition((rect.x() + 40, bottom - rect.y() - 40 - min(700, rect.height() - 80)))
            yield from sleep(0.8)
            record["screen"] = screens[-1].name()
        yield from wait_until(lambda: self.tab.activeInterfaceRootWidget() is not None, "viewer widget")
        panel = self.tab.activeInterfaceRootWidget()
        yield from wait_until(lambda: panel.isVisible(), "viewer visible", 5)
        self.paints = 0
        panel.canvas.framePainted.connect(self.count_paint)
        self.rop.parm("ipr_samples").set(8)
        panel.auto.setChecked(False)
        panel.set_rop(self.rop)
        panel.queue_render()
        yield from wait_until(
            lambda: panel.entry is not None
            and panel.entry.session.status()["state"] == "complete"
            and not panel.canvas.image.isNull(),
            "complete render",
        )
        yield from sleep(0.5)
        self.phase_started = time.monotonic()
        record["docked_complete"] = [self.sample(panel, "docked, complete (information only)")]
        for state, how in (
            ("complete", "call"),
            ("complete", "button click"),
            ("refining", "call"),
            ("refining", "button click"),
        ):
            # Park the pointer in the middle of the viewer: no edge toolbar is revealed.
            QtGui.QCursor.setPos(panel.canvas.mapToGlobal(panel.canvas.rect().center()))
            if state == "refining" and how == "call":
                self.rop.parm("ipr_samples").set(100000)
                panel.queue_render()
                yield from wait_until(
                    lambda: panel.entry.session.status()["state"] == "rendering" and not panel.canvas.image.isNull(),
                    "refining render",
                )
            yield from sleep(0.5)
            name = state + ("" if how == "call" else ", button click")
            progress(mode, layout, "enter", name)
            self.phase_started = time.monotonic()
            self.paint_times, self.expose_times = [], []
            contexts = getattr(panel.canvas, "gl_contexts", None)
            self.enter(panel, how)
            window = panel._fullscreen
            entered = round(time.monotonic() - self.phase_started, 3)
            handle = window.windowHandle()
            exposed_at_return = handle.isExposed()
            handle.installEventFilter(self.filter)
            # DWM keeps composing the fullscreen window only if it is not an
            # OpenGL window of exactly a screen's size: it must carry WS_BORDER
            # (or not cover a screen exactly). A grab cannot see DWM bypass.
            record[name + " window"] = border = fullscreen_border(window)
            if border["opengl"] and not border["ws_border"] and border["covers_screen_exactly"]:
                self.fail(mode, "fullscreen " + name + ": borderless window of exactly a screen's size", border)
            first = yield from self.series(panel, "fullscreen " + name, 1.2, 0.02)
            # Measured from when entering returned: Houdini's help indexer (a Python
            # thread) can hold the interpreter for a second while the click is
            # being processed, before the viewer runs at all.
            appeared = next((round(s["t"] - entered, 3) for s in first if not s["blank"]), None)
            record[name + " enter"] = first
            record[name + " first image seconds"] = appeared
            record[name + " timeline"] = dict(
                enter_returned=entered,
                exposed_at_return=exposed_at_return,
                expose_events=list(self.expose_times),
                paints=list(self.paint_times),
                contexts_created=None if contexts is None else panel.canvas.gl_contexts - contexts,
            )
            handle.removeEventFilter(self.filter)
            self.expose_times = None
            if appeared is None or appeared > FIRST_IMAGE_SECONDS:
                self.fail(mode, "fullscreen " + name + ": image not shown without a toolbar reveal", first[:6])
            settled = [s for s in first if s["t"] - entered >= FIRST_IMAGE_SECONDS]
            record[name + " blank frames on entry"] = sum(s["blank"] for s in first)
            blanks = [s for s in settled if s["blank"]]
            if blanks:
                self.fail(mode, "fullscreen " + name + ": blank frames after it first appeared", blanks[:4])
            if state == "complete" and settled and min(s["mean_difference"] for s in settled) > 1.0:
                self.fail(mode, "fullscreen " + name + ": screen differs from the classic drawing", settled[-1])
            if state == "complete" and how == "call":
                # Cover the idle fullscreen image with another window and uncover it:
                # the window is exposed again with no new frame to draw. The image
                # must come back by itself, without a toolbar reveal.
                cover = QtWidgets.QWidget(
                    None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
                )
                cover.setAutoFillBackground(True)
                palette = cover.palette()
                palette.setColor(QtGui.QPalette.Window, QtGui.QColor(0, 128, 0))
                cover.setPalette(palette)
                cover.setGeometry(window.geometry())
                cover.show()
                yield from sleep(0.4)
                cover.hide()
                cover.deleteLater()
                self.phase_started = time.monotonic()
                self.paint_times = []
                uncovered = yield from self.series(panel, "uncovered " + name, 1.0, 0.02)
                record[name + " uncovered"] = uncovered
                record[name + " uncovered paints"] = list(self.paint_times)
                blanks = [s for s in uncovered if s["blank"] or s["mean_difference"] > 1]
                record[name + " uncovered blank frames"] = len(blanks)
                if blanks:
                    self.fail(mode, "fullscreen " + name + ": wrong image after being uncovered", blanks[:4])
            if how == "call":
                # A top-level popup (as a menu or tooltip of the edge toolbars) must
                # show above the fullscreen window. On Windows, DWM does not compose
                # other top-level windows over a fullscreen OpenGL window (Qt's
                # "Fullscreen OpenGL Based Windows" note).
                popup = QtWidgets.QWidget(None, QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint)
                popup.setAutoFillBackground(True)
                palette = popup.palette()
                palette.setColor(QtGui.QPalette.Window, QtGui.QColor(255, 0, 255))
                popup.setPalette(palette)
                centre = panel.canvas.mapToGlobal(panel.canvas.rect().center())
                popup.setGeometry(centre.x() - 60, centre.y() - 60, 120, 120)
                popup.show()
                popup.raise_()
                yield from sleep(0.4)
                screen = QtWidgets.QApplication.screenAt(centre)
                geometry = screen.geometry()
                shot = pixels(
                    screen.grabWindow(
                        0, centre.x() - 30 - geometry.x(), centre.y() - 30 - geometry.y(), 60, 60
                    ).toImage(),
                    1,
                )
                magenta = float(((shot[:, :, 0] > 230) & (shot[:, :, 1] < 30) & (shot[:, :, 2] > 230)).mean())
                popup.hide()
                popup.deleteLater()
                record[name + " popup visible fraction"] = round(magenta, 3)
                if magenta < 0.9:
                    self.fail(mode, "fullscreen " + name + ": a top-level popup is not shown above it", magenta)
                yield from sleep(0.3)
            if state == "refining":
                steady = yield from self.series(panel, "fullscreen " + name, 2.0, 0.02)
                for overlay_name in ("top", "bottom"):
                    overlay = getattr(window, overlay_name)
                    window.reveal(overlay)
                    shown = yield from self.series(panel, "revealed " + overlay_name, 0.6, 0.02)
                    window.hide_controls()
                    hidden = yield from self.series(panel, "hidden " + overlay_name, 0.6, 0.02)
                    steady += shown + hidden
                record[name + " steady"] = steady
                blanks = [s for s in steady if s["blank"]]
                record[name + " blank frames"] = len(blanks)
                record[name + " frames"] = len(steady)
                if blanks:
                    self.fail(mode, "fullscreen " + name + ": blank frames", blanks[:4])
            progress(mode, layout, "leave", name)
            panel.close_fullscreen()
            self.phase_started = time.monotonic()
            # Docked, other applications can cover Houdini's main window on the
            # artist's desktop: the screen grab there is information only. The
            # canvas must still draw the image at once.
            record[name + " back"] = yield from self.series(panel, "docked after " + name, 0.5, 0.1)
            drawn = panel.canvas.grab().toImage()
            if drawn.isNull() or float((pixels(drawn).max(axis=2) < 10).mean()) > 0.9:
                self.fail(mode, "docked after fullscreen " + name + ": canvas draws black")
        progress(mode, layout, "close")
        panel.cancel()
        (self.tab.floatingPanel() if layout == "floating" else self.tab).close()
        self.tab = None
        registry.close_all()
        yield from sleep(0.5)

    def run(self):
        for mode, layout in LAYOUTS:
            yield from self.run_mode(mode, layout)
            self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.driver.stop()
        try:
            if self.tab is not None:
                self.tab.close()
            registry.close_all()
        except Exception:
            self.report["notes"].append("cleanup: " + traceback.format_exc(limit=2))
        if error:
            self.report["failures"].append(dict(problem="probe", detail=error))
        self.report["result"] = "failed" if self.report["failures"] else "passed"
        self.report["error"] = error
        self.save()
        sys.__stderr__.write("FULLSCREEN_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n")
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if self.report["failures"] else 0, suppress_save_prompt=True))


probe = Probe()
