# SPDX-License-Identifier: Apache-2.0
"""Cycles viewer display modes (CYCLES_HOUDINI_VIEWER_DISPLAY): gpu against classic.

On Houdini 22.0.508 for Windows, a new Cycles Render tab beside Scene View
must display its own canvas color on the desktop, both on first open and
after switching to Scene View and back. Reading the OpenGL framebuffer
alone misses this failure because its pixels can be correct while the
desktop still shows the scene.

A docked viewer in gpu mode renders a small scene. The viewer root must be a
native window and its OpenGL canvas an ordinary child inside it, draw without a
fallback, and match the classic QPainter canvas pixel for pixel (exactly)
on the same converted frame and view: fit, 1:1, zoomed and panned, A/B
compare wipe, region drag rectangle, no image, and a synthetic straight-alpha
image over the checker, and its scene-linear composites (one over a solid
background, two over the checker, a light-cell composite that differs from the
dark one, transparent outside a converted region; also in the A/B wipe).
Image-only fullscreen in and out keeps drawing (the
OpenGL context is recreated in the new window and released from the old one),
wheel and key navigation reach the canvas, and closing the pane tab releases
the canvas's textures and shaders. Writes only the GUI report and one stderr
line; never saves.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

os.environ["CYCLES_HOUDINI_VIEWER_DISPLAY"] = "gpu"

import hou  # noqa: E402
import numpy as np  # noqa: E402
import shiboken6  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402
from cycles_houdini.ipr import registry  # noqa: E402
from cycles_houdini.device_selection import set_devices  # noqa: E402
from cycles_houdini.viewer import panel as viewer_panel  # noqa: E402
from cycles_houdini.viewer import viewer_display  # noqa: E402

WAIT = 40.0
GpuCanvas = viewer_display.gpu_canvas_class()
paints = {"gpu": 0}
original_paint = GpuCanvas.paintGL


def counted_paint(self):
    paints["gpu"] += 1
    return original_paint(self)


GpuCanvas.paintGL = counted_paint


def progress(*parts):
    sys.__stderr__.write("VIEWER_DISPLAY " + " ".join(str(p) for p in parts) + "\n")
    sys.__stderr__.flush()


def sleep(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        yield


def wait_until(predicate, what, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for " + what)
        yield


def rgba_array(image):
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return data[:, : image.width() * 4].reshape(image.height(), image.width(), 4).astype(np.int16).copy()


def rgb_array(image):
    return rgba_array(image)[:, :, :3]


def difference(first, second):
    a, b = rgb_array(first), rgb_array(second)
    if a.shape != b.shape:
        return dict(shape_mismatch=[list(a.shape), list(b.shape)])
    delta = np.abs(a - b).max(axis=2)
    over = np.argwhere(delta > 1)
    return dict(
        shape=list(a.shape[:2]),
        max=int(delta.max()),
        over_1=int(len(over)),
        equal_fraction=round(float((delta == 0).mean()), 6),
        first_over=[int(v) for v in over[0]] if len(over) else None,
    )


def synthetic_image():
    rng = np.random.default_rng(7)
    pixels = rng.integers(0, 256, (48, 64, 4), dtype=np.uint8)
    pixels[:, :16, 3] = 255  # Opaque band.
    pixels[:, 16:24, 3] = 0  # Fully transparent band, RGB kept.
    data = pixels.tobytes()  # QImage(bytes) does not own them: copy while they are alive.
    image = QtGui.QImage(data, 64, 48, 64 * 4, QtGui.QImage.Format_RGBA8888).copy()
    image.setText("cycles:pixel_aspect", "1")
    return image


def synthetic_composites(image, backgrounds):
    """Opaque composites inside a converted region (rows 8..39), transparent outside; one per background."""
    result = []
    for index, _ in enumerate(backgrounds):
        rng = np.random.default_rng(11 + index)
        pixels = rng.integers(0, 256, (48, 64, 4), dtype=np.uint8)
        pixels[..., 3] = 0
        pixels[8:40, :, 3] = 255
        pixels[:8] = 0
        pixels[40:] = 0
        data = pixels.tobytes()
        result.append(QtGui.QImage(data, 64, 48, 64 * 4, QtGui.QImage.Format_RGBA8888).copy())
    return {image.cacheKey(): (tuple(backgrounds), result)}


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
        self.report = dict(result="running", device=self.device, failures=[], notes=[], comparisons={})
        self.desktop = hou.ui.curDesktop()
        geo = hou.node("/obj").createNode("geo", "display_subject", run_init_scripts=False)
        sphere = geo.createNode("sphere")
        sphere.parm("type").set("polymesh")
        sphere.setDisplayFlag(True)
        sphere.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "display_camera")
        camera.parmTuple("t").set((0.3, 0.2, 4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "display_render")
        set_devices(self.rop, self.device)
        self.rop.setParms(
            dict(
                camera=camera.path(),
                resx=320,
                resy=180,
                ipr_samples=8,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
            )
        )
        self.started = time.monotonic()
        self.finished = False
        self.work = self.run()
        self.driver = QtCore.QTimer()
        self.driver.setInterval(16)
        self.driver.timeout.connect(self.on_drive)
        self.driver.start()

    def check(self, ok, message, detail=None):
        if not ok:
            self.report["failures"].append(message + ("" if detail is None else ": " + json.dumps(detail, default=str)))

    def on_drive(self):
        if self.finished:
            return
        try:
            if time.monotonic() - self.started > 150:
                raise TimeoutError("Probe exceeded 150 s")
            next(self.work)
        except StopIteration:
            self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def check_docked_pixels(self, previous_tab, canvas):
        tint = QtGui.QColor(37, 158, 219)
        original_colors = canvas.scene_colors

        def colors():
            theme, background = original_colors()
            return dict(theme, canvas=tint), background

        points = [
            canvas.mapToGlobal(QtCore.QPoint(round(canvas.width() * x), round(canvas.height() * y)))
            for x, y in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75))
        ]

        def sampled_colors():
            sampled = []
            main = hou.qt.mainWindow()
            for global_point in points:
                screen = QtWidgets.QApplication.screenAt(global_point)
                owner = QtWidgets.QApplication.widgetAt(global_point)
                if screen is None or owner is None or owner.window() != main:
                    raise RuntimeError(f"Cannot sample unobscured Houdini desktop pixels at {global_point}")
                point = global_point - screen.geometry().topLeft()
                image = screen.grabWindow(0, point.x(), point.y(), 1, 1).toImage()
                if image.isNull():
                    raise RuntimeError(f"Desktop capture did not return pixels at {global_point}")
                sampled.append(image.pixelColor(0, 0).name())
            return sampled

        canvas.scene_colors = colors
        try:
            canvas.update()
            yield from sleep(0.4)
            first = sampled_colors()
            previous_tab.setIsCurrentTab()
            yield from sleep(0.25)
            scene = sampled_colors()
            self.tab.setIsCurrentTab()
            yield from sleep(0.4)
            reopened = sampled_colors()
            record = dict(expected=tint.name(), first=first, scene=scene, reopened=reopened)
            self.report["docked_pixels"] = record
            self.check(first.count(tint.name()) >= 2, "the first dock still shows Scene View", record)
            self.check(tint.name() not in scene, "the viewer remains above Scene View", record)
            self.check(reopened.count(tint.name()) >= 2, "the dock does not repaint after a tab switch", record)
        finally:
            del canvas.scene_colors
            canvas.update()

    def run(self):
        progress("mount")
        viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
        first_dock = sys.platform == "win32" and hou.applicationVersion() == (22, 0, 508)
        if first_dock:
            viewer.setIsCurrentTab()
            yield from sleep(0.25)
            self.tab = viewer.pane().createTab(
                hou.paneTabType.PythonPanel, python_panel_interface="cycles_houdini_render"
            )
        else:
            editor = self.desktop.paneTabOfType(hou.paneTabType.NetworkEditor)
            pane = (editor or viewer).pane().splitHorizontally()
            self.tab = pane.tabs()[0].setType(hou.paneTabType.PythonPanel)
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        yield from wait_until(lambda: self.tab.activeInterfaceRootWidget() is not None, "viewer widget")
        panel = self.panel = self.tab.activeInterfaceRootWidget()
        yield from wait_until(lambda: panel.isVisible(), "viewer visible", 5)
        canvas = panel.canvas
        if first_dock:
            yield from self.check_docked_pixels(viewer, canvas)
        main = hou.qt.mainWindow()
        handle = canvas.windowHandle()
        self.report["canvas"] = dict(
            cls=type(canvas).__name__,
            mode=canvas.display_mode,
            native=canvas.testAttribute(QtCore.Qt.WA_NativeWindow),
            win_id=bool(canvas.internalWinId()),
            surface=str(handle.surfaceType()) if handle is not None else None,
            handle_parent_is_main=bool(handle is not None and handle.parent() is main.windowHandle()),
            handle_parent=repr(handle.parent()) if handle is not None else None,
            main_handle=repr(main.windowHandle()),
            note=getattr(canvas, "display_note", ""),
            panel_native=bool(panel.internalWinId()),
            main_native_children=sum(
                1 for w in main.findChildren(QtWidgets.QWidget) if w.internalWinId() and w.objectName() != ""
            ),
        )
        self.check(isinstance(canvas, GpuCanvas), "canvas is not the OpenGL canvas", self.report["canvas"])
        self.check(canvas.display_mode == "gpu", "display mode", canvas.display_mode)
        # As Houdini's RE_Window: the viewer root is the one native window; the
        # OpenGL canvas inside it is an ordinary child, never a native window
        # Houdini's pane layout could move out of its slot.
        self.check(bool(panel.internalWinId()), "viewer root has no native window", self.report["canvas"])
        self.check(not canvas.internalWinId(), "canvas was made native", self.report["canvas"])
        holder = canvas._resources

        progress("render")
        panel.set_rop(self.rop)
        panel.queue_render()

        def complete():
            entry = panel.entry
            if entry is None:
                return False
            status = entry.session.status()
            assert status["state"] != "failed", status
            return status["state"] == "complete" and not status.get("pending_update") and panel._live_generation

        yield from wait_until(complete, "render to complete")
        yield from sleep(0.5)
        self.check(holder[0] is not None, "no OpenGL resources after drawing a frame")
        self.check(not canvas.gl_failure, "OpenGL drawing failed", canvas.gl_failure)
        self.report["gl_info"] = canvas.gl_info
        self.report["tooltip_line"] = viewer_display.describe(canvas)
        self.check("Viewer display: gpu" in panel.info.toolTip(), "mode missing from diagnostics tooltip")
        self.report["sharing"] = self.sharing_probe(canvas)

        progress("compare")
        reference = viewer_panel.ImageCanvas()
        reference._viewer_colors = getattr(canvas, "_viewer_colors", None)
        reference._checker = canvas._checker
        reference._checker_mask = canvas._checker_mask
        reference._checker_colors = canvas._checker_colors
        reference.setFont(canvas.font())
        reference.setPalette(canvas.palette())
        reference.resize(canvas.size())
        live = canvas.image

        def same_state():
            for name in (
                "image",
                "compare_image",
                "compare_fraction",
                "full_size",
                "view_mode",
                "_scale",
                "_pan",
                "checker_enabled",
                "background_enabled",
                "background_color",
                "drag_start",
                "drag_end",
                "keep_image_size",
                "_reference_height",
                "linked_reference",
                "composites",
            ):
                setattr(reference, name, getattr(canvas, name))

        def compare(name):
            same_state()
            gpu = canvas.grabFramebuffer()
            classic = reference.grab().toImage()
            result = difference(gpu, classic)
            self.report["comparisons"][name] = result
            self.check(result.get("max", 99) == 0, "pixels differ from classic: " + name, result)
            return gpu

        canvas.fit_image()
        yield
        compare("fit")
        canvas.actual_size()
        yield
        gpu = compare("actual")
        # 1:1, opaque pixels: the framebuffer holds exactly the converted image
        # (QPainter places an unscaled image at the rounded position).
        rect = canvas.image_rect()
        crop = gpu.copy(viewer_display.qround(rect.x()), viewer_display.qround(rect.y()), live.width(), live.height())
        source = rgb_array(live)
        alpha = rgba_array(live)[:, :, 3]
        if crop.size() == live.size():
            delta = np.abs(rgb_array(crop) - source).max(axis=2)[alpha == 255]
            self.report["comparisons"]["actual_vs_converted_image"] = dict(
                opaque_pixels=int(delta.size), max=int(delta.max()) if delta.size else None
            )
            self.check(delta.size == 0 or delta.max() == 0, "1:1 opaque pixels differ from the converted image")
        else:
            self.report["notes"].append(f"1:1 crop {crop.size()} differs from image {live.size()}")
        canvas.set_zoom(370)
        canvas._pan = QtCore.QPointF(-37.25, 21.5)
        canvas.update()
        yield
        compare("zoom_370_pan")
        canvas.set_zoom(33)
        yield
        compare("zoom_33")
        canvas.fit_image()
        canvas.compare_image = live.mirrored(True, False)
        canvas.compare_fraction = 0.37
        yield
        compare("compare_wipe")
        canvas.compare_image = QtGui.QImage()
        canvas.drag_start = QtCore.QPointF(20.5, 15)
        canvas.drag_end = QtCore.QPointF(140, 96.5)
        yield
        compare("region_drag_rectangle")
        canvas.drag_start = canvas.drag_end = None
        canvas.image = synthetic_image()
        canvas.full_size = (64, 48)
        canvas.set_zoom(500)
        yield
        compare("synthetic_alpha_checker")
        canvas.checker_enabled = False
        canvas.background_enabled = True
        canvas.background_color = QtGui.QColor(200, 30, 90)
        yield
        compare("synthetic_alpha_background")
        canvas.composites = synthetic_composites(canvas.image, canvas.display_backgrounds())
        yield
        compare("synthetic_composite_background")
        canvas.checker_enabled = True
        canvas.background_enabled = False
        canvas.composites = synthetic_composites(canvas.image, canvas.display_backgrounds())
        yield
        compare("synthetic_composites_checker")
        canvas.set_zoom(130)
        canvas._pan = QtCore.QPointF(-3.25, 7.5)
        yield
        compare("synthetic_composites_checker_zoom_130_pan")
        canvas.compare_image = canvas.image.mirrored(True, False)
        canvas.composites.update(synthetic_composites(canvas.compare_image, canvas.display_backgrounds()))
        canvas.compare_fraction = 0.41
        yield
        compare("synthetic_composites_compare_wipe")
        canvas.composites = {key: (((1, 2, 3),), value[1][:1]) for key, value in canvas.composites.items()}
        yield
        compare("synthetic_composites_stale_backgrounds")  # Not shown: display-space blend only.
        canvas.compare_image = QtGui.QImage()
        canvas.composites = {}
        canvas.set_zoom(500)
        canvas.image = QtGui.QImage()
        canvas.full_size = (0, 0)
        yield
        compare("no_image_text")
        canvas.image = live
        canvas.full_size = (live.width(), live.height())
        canvas.fit_image()
        reference.deleteLater()

        progress("input")
        QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_F)
        self.check(canvas.view_mode == "actual", "F key did not reach the canvas", canvas.view_mode)
        QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_Home)
        self.check(canvas.view_mode == "fit", "Home key did not reach the canvas", canvas.view_mode)
        center = canvas.rect().center()
        wheel = QtGui.QWheelEvent(
            QtCore.QPointF(center),
            QtCore.QPointF(canvas.mapToGlobal(center)),
            QtCore.QPoint(),
            QtCore.QPoint(0, 120),
            QtCore.Qt.NoButton,
            QtCore.Qt.NoModifier,
            QtCore.Qt.NoScrollPhase,
            False,
        )
        QtWidgets.QApplication.sendEvent(canvas, wheel)
        self.check(canvas.view_mode == "manual", "wheel did not zoom", canvas.view_mode)
        canvas.fit_image()
        # Ctrl+right drag draws the box-zoom rectangle (no render change, unlike a region).
        QtTest.QTest.mousePress(canvas, QtCore.Qt.RightButton, QtCore.Qt.ControlModifier, center)
        QtTest.QTest.mouseMove(canvas, center + QtCore.QPoint(30, 20))
        drawing = canvas.drag_start is not None
        QtTest.QTest.mouseRelease(
            canvas, QtCore.Qt.RightButton, QtCore.Qt.ControlModifier, center + QtCore.QPoint(30, 20)
        )
        self.check(drawing, "ctrl+right drag did not start the box rectangle")
        self.check(canvas.view_mode == "manual", "box zoom did not zoom", canvas.view_mode)
        self.report["focus_after_click"] = QtWidgets.QApplication.focusWidget() is canvas
        canvas.fit_image()
        yield from sleep(0.3)

        progress("fullscreen")
        contexts = canvas.gl_contexts
        before = paints["gpu"]
        panel.open_fullscreen()
        yield from wait_until(lambda: panel._fullscreen is not None and panel._fullscreen.isFullScreen(), "fullscreen")
        yield from sleep(0.8)
        window = panel._fullscreen
        record = dict(
            canvas_in_window=canvas.window() is window,
            canvas_visible=canvas.isVisible(),
            paints=paints["gpu"] - before,
            contexts_created=canvas.gl_contexts - contexts,
            overlays_native=bool(window.top.internalWinId()) or bool(window.bottom.internalWinId()),
            gl_failure=canvas.gl_failure,
            screen_size=[canvas.width(), canvas.height()],
        )
        grabbed = canvas.grabFramebuffer()
        samples = rgb_array(grabbed)[::37, ::37].reshape(-1, 3)
        record["grab_colours"] = int(len(np.unique(samples, axis=0)))
        record["grab_size"] = [grabbed.width(), grabbed.height()]
        window.reveal(window.top)
        yield from sleep(0.2)
        record["top_overlay_above_canvas"] = window.top.isVisible()
        self.report["fullscreen"] = record
        self.check(record["canvas_in_window"] and record["canvas_visible"], "canvas not shown fullscreen", record)
        self.check(record["paints"] > 0, "no paint in fullscreen", record)
        self.check(not record["gl_failure"], "OpenGL failed in fullscreen", record)
        self.check(not record["overlays_native"], "fullscreen overlays were made native", record)
        before = paints["gpu"]
        contexts = canvas.gl_contexts
        panel.close_fullscreen()
        yield from wait_until(lambda: panel._fullscreen is None, "fullscreen closed")
        yield from sleep(0.8)
        back = dict(
            canvas_in_panel=panel.isAncestorOf(canvas),
            canvas_visible=canvas.isVisible(),
            paints=paints["gpu"] - before,
            contexts_created=canvas.gl_contexts - contexts,
            gl_failure=canvas.gl_failure,
            native=bool(canvas.internalWinId()),
        )
        self.report["fullscreen_back"] = back
        self.check(back["canvas_in_panel"] and back["canvas_visible"], "canvas not back in panel", back)
        self.check(back["paints"] > 0, "no paint after fullscreen", back)
        self.check(not back["gl_failure"] and not back["native"], "after fullscreen", back)
        self.check(holder[0] is not None, "no OpenGL resources after fullscreen")

        progress("close")
        self.tab.close()
        self.tab = None
        yield from wait_until(lambda: not shiboken6.isValid(panel) or panel._disposed, "viewer disposed", 10)
        yield from sleep(0.5)
        self.report["closed"] = dict(resources_released=holder[0] is None)
        self.check(holder[0] is None, "OpenGL resources kept after close")

    def sharing_probe(self, canvas):
        """Could a converter context share textures with the canvas? (For removing the readback.)"""
        result = {}
        try:
            share = QtGui.QOpenGLContext.globalShareContext()
            result["global_share_context"] = share is not None
            result["canvas_shares_global"] = bool(
                share is not None and QtGui.QOpenGLContext.areSharing(canvas.context(), share)
            )
            surface = QtGui.QOffscreenSurface()
            surface.setFormat(canvas.context().format())
            surface.create()
            plain = QtGui.QOpenGLContext()
            plain.setFormat(surface.format())
            plain.create()
            result["plain_context_shares_canvas"] = QtGui.QOpenGLContext.areSharing(plain, canvas.context())
            shared = QtGui.QOpenGLContext()
            shared.setFormat(surface.format())
            shared.setShareContext(share)
            result["share_context_created"] = shared.create()
            result["shared_context_shares_canvas"] = QtGui.QOpenGLContext.areSharing(shared, canvas.context())
            shared.deleteLater()
            plain.deleteLater()
            surface.destroy()
        except Exception as error:
            result["error"] = str(error)
        return result

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.driver.stop()
        try:
            if getattr(self, "tab", None) is not None:
                self.tab.close()
            registry.close_all()
        except Exception:
            self.report["notes"].append("cleanup: " + traceback.format_exc(limit=2))
        if error:
            self.report["failures"].append("probe: " + error)
        self.report["result"] = "failed" if self.report["failures"] else "passed"
        self.report["error"] = error
        self.save()
        sys.__stderr__.write(
            "VIEWER_DISPLAY_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n"
        )
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if self.report["failures"] else 0, suppress_save_prompt=True))


probe = Probe()
