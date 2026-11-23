# SPDX-License-Identifier: Apache-2.0
"""Viewer display modes, docked and floating: Houdini main-thread latency while the viewport tumbles.

For each display mode (CYCLES_HOUDINI_VIEWER_DISPLAY = classic, native, gpu)
and layout (floating, docked), with the render refining and restarting on
object edits, it records the interval distribution of a 5 ms precise QTimer
(event-loop latency; ideal is about 1200 ticks per 6 s), viewer paint, poll
and present costs, presented frames, display readbacks (GPU to CPU), and
Houdini's main window: backing-store syncs (UpdateRequest) and the time spent
in them (mostly Houdini's own viewport drawing). houdini_surface_flush_by_viewer
counts syncs in which a viewer widget painted into a Houdini surface (docked:
the pane area's native RE_WindowDrawable, 3840x2043 here; floating: the
floating window), which Qt then composes and flushes; viewer_paint_own_surface
counts paints into the viewer's own native window (native and gpu canvases). The scene
viewport is tumbled programmatically every 16 ms (GeometryViewport.draw()
only queues a redraw, so its timing is not a draw cost). Writes only the GUI
report and one stderr line; never saves.
"""

from collections import Counter
import json
import math
import os
import sys
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.viewer import gpu_display, viewer_display
from cycles_houdini.viewer import panel as viewer_panel

PHASE_SECONDS = float(os.environ.get("CYCLES_HOUDINI_DOCK_PHASE_SECONDS", "6"))
RESOLUTION = [int(v) for v in os.environ.get("CYCLES_HOUDINI_DOCK_RESOLUTION", "960x540").split("x")]
MODES = ("classic", "native", "gpu")
STATUS_SECONDS = viewer_panel.STATUS_REFRESH_SECONDS
BASELINE_SECONDS = 6.0
WAIT_SECONDS = 30.0


def summary(values):
    if not values:
        return dict(count=0)
    ordered = sorted(values)

    def q(fraction):
        return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 3)

    return dict(
        count=len(ordered),
        mean=round(sum(ordered) / len(ordered), 3),
        p50=q(0.5),
        p95=q(0.95),
        p99=q(0.99),
        max=round(ordered[-1], 3),
    )


class Meter:
    """Per-phase counters; wrappers add into whichever phase is current."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.ticks = []
        self.draws = []
        self.costs = {}
        self.events = Counter()
        self.started = time.perf_counter()

    def cost(self, name, seconds):
        self.costs.setdefault(name, []).append(seconds * 1000.0)


meter = Meter()


def timed(name, function):
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            meter.cost(name, time.perf_counter() - started)

    wrapper.__name__ = function.__name__
    return wrapper


# Patch classes before Houdini creates the panel so Qt dispatches the wrappers.
viewer_panel.ImageCanvas.paintEvent = timed("canvas_paint_ms", viewer_panel.ImageCanvas.paintEvent)
viewer_panel.StatusLine.paintEvent = timed("status_paint_ms", viewer_panel.StatusLine.paintEvent)
viewer_panel.RenderPanel.poll = timed("poll_ms", viewer_panel.RenderPanel.poll)
viewer_panel.RenderPanel.present_available = timed("present_ms", viewer_panel.RenderPanel.present_available)
viewer_panel.ImageCanvas.merge_frame = staticmethod(timed("merge_frame_ms", viewer_panel.ImageCanvas.merge_frame))
GpuCanvas = viewer_display.gpu_canvas_class()
GpuCanvas.paintGL = timed("canvas_paint_ms", GpuCanvas.paintGL)
# Worker thread: colour conversion including the readback.
gpu_display.GpuContext.convert = timed("convert_worker_ms", gpu_display.GpuContext.convert)


def native_surface(widget):
    return widget if widget.internalWinId() else widget.nativeParentWidget()


class EventCounter(QtCore.QObject):
    KINDS = {
        QtCore.QEvent.UpdateRequest: "update_request",
        QtCore.QEvent.LayoutRequest: "layout_request",
        QtCore.QEvent.Paint: "paint",
    }

    def __init__(self, probe):
        super().__init__(QtWidgets.QApplication.instance())
        self.probe = probe
        self.sync = 0
        self.flushed_viewer = -1
        self.dispatching = False

    def eventFilter(self, obj, event):
        main = self.probe.main
        if event.type() == QtCore.QEvent.UpdateRequest and isinstance(obj, QtWidgets.QWidget) and obj.isWindow():
            self.sync += 1  # One backing-store sync of a top-level window.
        if event.type() == QtCore.QEvent.UpdateRequest and obj is main and not self.dispatching:
            # Time the main window's sync: paints, composition and flushes of
            # every surface in it, Houdini's own viewports included.
            self.dispatching = True
            started = time.perf_counter()
            try:
                obj.event(event)
            finally:
                self.dispatching = False
            meter.cost("main_window_sync_ms", time.perf_counter() - started)
            meter.events["update_request:main_window_tlw"] += 1
            return True
        if event.type() == QtCore.QEvent.Paint and isinstance(obj, QtWidgets.QWidget):
            try:
                panel = self.probe.panel
                if panel is not None and (obj is panel or panel.isAncestorOf(obj)):
                    surface = native_surface(obj)
                    if surface is not None and (surface is panel or panel.isAncestorOf(surface)):
                        meter.events["viewer_paint_own_surface"] += 1
                    elif self.flushed_viewer != self.sync:
                        # A viewer widget painted into a Houdini surface (the
                        # pane area's RE_WindowDrawable or a floating window):
                        # that surface is flushed and composed in this sync.
                        self.flushed_viewer = self.sync
                        meter.events["houdini_surface_flush_by_viewer"] += 1
                        name = type(surface).__name__ + ":" + (surface.objectName() if surface is not None else "")
                        meter.events["surface:" + name] += 1
            except RuntimeError:
                pass
        kind = self.KINDS.get(event.type())
        if kind is not None and isinstance(obj, QtWidgets.QWidget):
            try:
                panel = self.probe.panel
                if panel is not None and (obj is panel or panel.isAncestorOf(obj)):
                    where = "viewer"
                elif obj is self.probe.main or (self.probe.main is not None and obj.window() is self.probe.main):
                    where = "main_window_tlw" if obj is self.probe.main else "main_window_child"
                else:
                    where = "other_window_tlw" if obj.isWindow() else "other"
                meter.events[kind + ":" + where] += 1
            except RuntimeError:
                pass
        return False


def describe_window(widget):
    if widget is None:
        return None
    handle = widget.windowHandle()
    info = dict(
        cls=type(widget).__name__,
        meta=widget.metaObject().className(),
        visible=widget.isVisible(),
        minimized=widget.isMinimized(),
        size=[widget.width(), widget.height()],
        dpr=widget.devicePixelRatioF(),
        native=widget.testAttribute(QtCore.Qt.WA_NativeWindow),
        surface=str(handle.surfaceType()) if handle is not None else None,
        flags=str(widget.windowFlags()),
    )
    try:
        from PySide6 import QtOpenGLWidgets

        info["qopenglwidgets"] = len(widget.findChildren(QtOpenGLWidgets.QOpenGLWidget))
    except Exception as error:
        info["qopenglwidgets"] = "unavailable: " + str(error)
    classes = Counter()
    native = 0
    for child in widget.findChildren(QtWidgets.QWidget):
        if child.testAttribute(QtCore.Qt.WA_NativeWindow) or child.internalWinId():
            native += 1
            classes[child.metaObject().className()] += 1
    info["native_children"] = native
    info["native_child_classes"] = dict(classes.most_common(12))
    return info


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
        self.report = dict(
            result="running",
            device=self.device,
            phase_seconds=PHASE_SECONDS,
            phases=[],
            notes=[],
        )
        self.main = hou.qt.mainWindow()
        self.panel = self.tab = self.floating = None
        self.readbacks = 0
        self.desktop = hou.ui.curDesktop()
        self.viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
        assert self.viewer is not None, "Desktop has no Scene Viewer"
        self.viewport = self.viewer.curViewport()
        geo = hou.node("/obj").createNode("geo", "dock_subject", run_init_scripts=False)
        sphere = geo.createNode("sphere")
        sphere.parm("type").set("polymesh")
        sphere.setDisplayFlag(True)
        sphere.setRenderFlag(True)
        ground = hou.node("/obj").createNode("geo", "dock_ground", run_init_scripts=False)
        grid = ground.createNode("grid")
        grid.setDisplayFlag(True)
        grid.setRenderFlag(True)
        ground.parm("ty").set(-1)
        self.subject = geo
        camera = hou.node("/obj").createNode("cam", "dock_camera")
        camera.parmTuple("t").set((0, 1, 6))
        camera.parmTuple("r").set((-8, 0, 0))
        self.rop = hou.node("/out").createNode("cycles_ipr", "dock_render")
        set_devices(self.rop, self.device)
        self.rop.setParms(
            dict(
                camera=camera.path(),
                resx=RESOLUTION[0],
                resy=RESOLUTION[1],
                ipr_samples=4,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
            )
        )
        try:
            self.viewport.frameAll()
        except Exception:
            self.report["notes"].append("frameAll failed: " + traceback.format_exc(limit=1))
        self.report["main_window"] = describe_window(self.main)
        self.report["screens"] = [
            dict(
                name=s.name(),
                size=[s.size().width(), s.size().height()],
                dpr=s.devicePixelRatio(),
                hz=s.refreshRate(),
            )
            for s in QtGui.QGuiApplication.screens()
        ]
        self.counter = EventCounter(self)
        QtWidgets.QApplication.instance().installEventFilter(self.counter)
        self.angle = 0.0
        self.last_tick = None
        self.latency = QtCore.QTimer(self.main)
        self.latency.setTimerType(QtCore.Qt.PreciseTimer)
        self.latency.setInterval(5)
        self.latency.timeout.connect(self.on_latency)
        self.driver = QtCore.QTimer(self.main)
        self.driver.setTimerType(QtCore.Qt.PreciseTimer)
        self.driver.setInterval(16)
        self.driver.timeout.connect(self.on_drive)
        self.steps = self.plan()
        self.step = None
        self.step_started = time.monotonic()
        self.started = time.monotonic()
        self.edit_at = 0.0
        self.measuring = None
        self.finished = False
        self.step_initialized = False
        self.placeholder = None
        self.settle_until = 0.0
        self.latency.start()
        self.driver.start()

    # Phase plan -----------------------------------------------------------
    def plan(self):
        # A docked panel is mounted once without rendering to size the split;
        # a Python Shell then holds its place, so the scene viewport keeps the
        # same size in every phase and the floating window covers that area.
        # Floating runs first, since early phases in a fresh session are
        # noisier. "docked-native" repeats the docked panel after giving it
        # its own native window. Each layout restarts the render, so refine
        # phases start from the same progressive state. "refine-every-poll"
        # refreshes the status counters on every poll (the previous behaviour).
        # Layouts are "<floating|docked>:<display mode>"; the mode is read when
        # the panel is created, so every layout mounts a new panel.
        steps = [("mount", "docked-probe", None, 0), ("unmount", "docked-probe", None, 0)]
        steps.append(("measure", "none", "baseline-start", BASELINE_SECONDS))
        for display in MODES:
            for place in ("floating", "docked"):
                layout = place + ":" + display
                steps += [
                    ("mount", layout, None, 0),
                    ("render_idle", layout, None, 0),
                    ("render_refine", layout, None, 0),
                    ("measure", layout, "refine", PHASE_SECONDS),
                    ("measure", layout, "edit", PHASE_SECONDS),
                    ("unmount", layout, None, 0),
                ]
        steps += [("measure", "none", "baseline-end", BASELINE_SECONDS), ("finish", None, None, 0)]
        return steps

    # Timers ---------------------------------------------------------------
    def on_latency(self):
        now = time.perf_counter()
        if self.last_tick is not None and self.measuring:
            meter.ticks.append((now - self.last_tick) * 1000.0)
        self.last_tick = now

    def tumble(self):
        try:
            camera = self.viewport.defaultCamera()
            self.angle += 1.5
            rotation = hou.hmath.buildRotate((-15, self.angle, 0)).extractRotationMatrix3()
            camera.setRotation(rotation)
            self.viewport.setDefaultCamera(camera)
            started = time.perf_counter()
            self.viewport.draw()
            meter.draws.append((time.perf_counter() - started) * 1000.0)
        except Exception:
            if "tumble_error" not in self.report:
                self.report["tumble_error"] = traceback.format_exc()

    def on_drive(self):
        try:
            now = time.monotonic()
            if now - self.started > 330:
                raise TimeoutError("Probe exceeded 330 s at step %r" % (self.step,))
            self.tumble()
            if self.step is None:
                self.advance()
            kind, layout, mode, seconds = self.step
            elapsed = now - self.step_started
            if kind == "measure":
                if mode.startswith("edit") and now >= self.edit_at:
                    self.edit_at = now + 0.05
                    self.subject.parm("tx").set(0.5 * math.sin(now * 3.0))
                if elapsed >= seconds:
                    self.record(layout, mode, elapsed)
                    self.advance()
            elif kind == "mount":
                if self.mount(layout):
                    self.advance()
            elif kind in ("render_idle", "render_refine"):
                if self.wait_render(kind, elapsed):
                    self.advance()
            elif kind == "unmount":
                self.unmount(layout)
                self.advance()
            elif kind == "finish":
                self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def advance(self):
        self.step = self.steps.pop(0)
        self.step_started = time.monotonic()
        kind, layout, mode, _ = self.step
        self.measuring = kind == "measure"
        self.step_initialized = False
        if self.measuring:
            viewer_panel.STATUS_REFRESH_SECONDS = STATUS_SECONDS
            meter.reset()
            self.readbacks = gpu_display.readbacks
            self.last_tick = None

    # Steps ----------------------------------------------------------------
    def floating_window(self):
        for floating in hou.ui.floatingPanels():
            for pane in floating.paneTabs():
                if pane.type() == hou.paneTabType.PythonPanel and pane.activeInterfaceRootWidget() is self.panel:
                    return floating, hou.qt.floatingPanelWindow(floating)
        return None, None

    def mount(self, layout):
        place, _, display = layout.partition(":")
        if self.tab is None:
            os.environ[viewer_display.ENVIRONMENT] = display or "classic"
            if layout.startswith("docked") and self.placeholder is not None:
                # The placeholder becomes the viewer again. Houdini re-shows the
                # released viewer of the earlier docked phase; it reloads itself.
                tab = self.placeholder.setType(hou.paneTabType.PythonPanel)
            elif layout.startswith("docked"):
                pane = self.viewer.pane()
                new_pane = pane.splitHorizontally()
                tab = new_pane.tabs()[0].setType(hou.paneTabType.PythonPanel)
                try:
                    pane.setSplitFraction(0.5)
                except Exception:
                    self.report["notes"].append("setSplitFraction failed")
            else:
                geometry = self.report.get("docked-probe_panel_geometry")
                size = tuple(geometry[2:]) if geometry else (900, 650)
                tab = self.desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=size)
            tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
            self.tab = tab
            return False
        layout = place
        self.panel = self.tab.activeInterfaceRootWidget()
        if self.panel is None or self.panel._disposed:
            return False  # Not created yet, or a re-shown released viewer still reloading.
        if layout == "floating" and not self.step_initialized:
            # Cover exactly the docked pane's area (now a Python Shell
            # placeholder), so the scene viewport keeps the same size.
            floating, _ = self.floating_window()
            if (floating is None or not self.panel.isVisible()) and time.monotonic() - self.step_started < 3:
                return False
            self.step_initialized = True
            self.settle_until = time.monotonic() + 0.75
            target = self.report.get("docked-probe_panel_geometry")
            if floating is not None and target:
                try:
                    origin = self.panel.mapToGlobal(QtCore.QPoint(0, 0))
                    x, y = floating.position()
                    w, h = floating.size()
                    floating.setPosition([int(x + target[0] - origin.x()), int(y + target[1] - origin.y())])
                    floating.setSize(
                        [int(w + target[2] - self.panel.width()), int(h + target[3] - self.panel.height())]
                    )
                except Exception:
                    self.report["notes"].append("floating placement failed: " + traceback.format_exc(limit=2))
            else:
                self.report["notes"].append("floating window not found for placement")
            return False
        if layout == "floating" and time.monotonic() < self.settle_until:
            return False
        origin = self.panel.mapToGlobal(QtCore.QPoint(0, 0))
        self.report[layout + "_panel_geometry"] = [
            origin.x(),
            origin.y(),
            self.panel.width(),
            self.panel.height(),
        ]
        self.report[layout + "_canvas_size"] = [
            self.panel.canvas.width(),
            self.panel.canvas.height(),
        ]
        self.report[layout + "_panel_window"] = describe_window(self.panel.window())
        self.report[layout + "_panel_window_is_main"] = self.panel.window() is self.main
        self.report[layout + "_viewport_size"] = list(self.viewport.size())
        if layout == "docked-probe":
            return True
        self.report.setdefault("canvas", {})[place + ":" + display] = dict(
            cls=type(self.panel.canvas).__name__,
            mode=self.panel.canvas.display_mode,
            native=bool(self.panel.canvas.internalWinId()),
            surface=str(self.panel.canvas.windowHandle().surfaceType())
            if self.panel.canvas.windowHandle() is not None
            else None,
        )
        self.panel.set_rop(self.rop)
        self.panel.auto.setChecked(True)
        self.rop.parm("ipr_samples").set(4)
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        return True

    def wait_render(self, kind, elapsed):
        if kind == "render_refine" and not self.step_initialized:
            self.step_initialized = True
            self.rop.parm("ipr_samples").set(1048576)
            self.panel.queue_render()
            return False
        entry = self.panel.entry
        if entry is None:
            assert elapsed < WAIT_SECONDS, dict(
                error="Viewer did not attach a session",
                info=self.panel.info.text(),
                lifetime=self.panel._lifetime.state,
                canvas_visible=self.panel.canvas.isVisible(),
            )
            return False
        status = entry.session.status()
        assert status["state"] != "failed", status
        if kind == "render_idle":
            done = status["state"] == "complete" and not status.get("pending_update") and self.panel._live_generation
        else:
            done = status["state"] == "rendering" and status.get("current_sample", 0) > 4
        assert done or elapsed < WAIT_SECONDS, dict(step=kind, status_state=status["state"])
        return bool(done)

    def record(self, layout, mode, elapsed):
        status = {}
        if self.panel is not None and self.panel.entry is not None:
            full = self.panel.entry.session.status()
            status = {k: full.get(k) for k in ("state", "generation", "current_sample")}
        ticks = meter.ticks
        frames = len(meter.costs.get("merge_frame_ms", []))
        main_thread = sum(sum(meter.costs.get(name, [])) for name in ("present_ms", "canvas_paint_ms"))
        canvas = getattr(self.panel, "canvas", None) if self.panel is not None else None
        phase = dict(
            display=getattr(canvas, "display_mode", None),
            frames_presented=frames,
            main_thread_ms_per_frame=round(main_thread / frames, 3) if frames else None,
            readbacks_per_second=round((gpu_display.readbacks - self.readbacks) / elapsed, 1),
            main_window_sync_ms_per_second=round(sum(meter.costs.get("main_window_sync_ms", [])) / elapsed, 2),
            gl_failure=getattr(canvas, "gl_failure", None),
            layout=layout,
            mode=mode,
            status_refresh_seconds=viewer_panel.STATUS_REFRESH_SECONDS,
            seconds=round(elapsed, 3),
            render=status,
            loop_interval_ms=summary(ticks),
            loop_stalls_over_20ms=sum(t > 20 for t in ticks),
            loop_stalls_over_50ms=sum(t > 50 for t in ticks),
            viewport_draw_ms=summary(meter.draws),
            costs={name: dict(summary(v), total_ms=round(sum(v), 1)) for name, v in meter.costs.items()},
            viewport_size=list(self.viewport.size()),
            panel_geometry=self.geometry(),
            events_per_second={k: round(v / elapsed, 1) for k, v in sorted(meter.events.items())},
        )
        self.report["phases"].append(phase)
        self.save()

    def geometry(self):
        if self.panel is None:
            return None
        origin = self.panel.mapToGlobal(QtCore.QPoint(0, 0))
        return dict(
            panel=[origin.x(), origin.y(), self.panel.width(), self.panel.height()],
            canvas=[self.panel.canvas.width(), self.panel.canvas.height()],
            window_is_main=self.panel.window() is self.main,
            panel_native=bool(self.panel.internalWinId()),
            canvas_native=bool(self.panel.canvas.internalWinId()),
        )

    def unmount(self, layout):
        if self.panel is not None:
            try:
                self.panel.stop_button.click()
            except Exception:
                pass
        tab, self.tab = self.tab, None
        try:
            if layout.startswith("docked"):
                # Keep the split so the viewport size is unchanged for the
                # floating phases; a Python Shell stands in for the viewer.
                self.placeholder = tab.setType(hou.paneTabType.PythonShell)
            else:
                floating, _ = self.floating_window()
                (floating or tab).close()
        except Exception:
            self.report["notes"].append("close failed: " + traceback.format_exc(limit=2))
        self.panel = None
        registry.close_all()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.driver.stop()
        self.latency.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self.counter)
        try:
            if self.tab is not None:
                floating, _ = self.floating_window()
                (floating or self.tab).close()
            registry.close_all()
        except Exception:
            self.report["notes"].append("cleanup: " + traceback.format_exc(limit=2))
        self.report["result"] = "failed" if error else "passed"
        self.report["error"] = error
        self.save()
        # The runner log keeps the tail of stderr; give it the whole report on one line.
        sys.__stderr__.write(
            "DOCK_LATENCY_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n"
        )
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


probe = Probe()
