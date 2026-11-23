# SPDX-License-Identifier: Apache-2.0
"""Profile real viewport redraw/handles with saved render settings and IPR off/on."""

from support.paths import artifacts_root

from native_test_runtime import runtime_executable
from collections import defaultdict
import cProfile
import hashlib
import json
import math
import os
from pathlib import Path
import pstats
import subprocess
import threading
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import (
    _native,
    device_selection,
    light_properties,
    light_viewer_state,
    native_scene,
    object_scene,
    registry,
)
from cycles_houdini.panel import ImageCanvas, RenderPanel
from cycles_houdini.viewer import viewer_display


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.source = Path(
            os.environ.get(
                "CYCLES_HOUDINI_PROFILE_SCENE", str(artifacts_root() / "examples/sop-fx-courtyard/Test.hiplc")
            )
        )
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        hou.hipFile.load(str(self.source), suppress_save_prompt=True, ignore_load_warnings=True)
        self.rop = hou.node(os.environ.get("CYCLES_HOUDINI_PROFILE_ROP", "/out/Test_2"))
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.overrides = json.loads(os.environ.get("CYCLES_HOUDINI_PROFILE_OVERRIDES", "{}"))
        self.rop.setParms(self.overrides)
        # Preserve saved sampling, motion blur, resolution and denoising.
        self.actor = hou.node("/obj/circle1") or hou.node("/obj/fx_courtyard")
        self.light = next(
            n
            for n in object_scene.selected(self.rop, hou.time(), lights=True)
            if not native_scene.world.is_environment(n)
        )
        self.original = {n.path(): n.evalParm("tx") for n in (self.actor, self.light)}
        self.original_matrices = {n.path(): n.worldTransform() for n in (self.actor, self.light)}
        self.original_channels = {
            n.path(): {name: n.parmTuple(name).eval() for name in ("t", "r", "s")} for n in (self.actor, self.light)
        }
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.viewport_window = None
        self.viewer.setPwd(hou.node("/obj"))
        self.viewer.curViewport().setCamera(hou.node(self.rop.evalParm("camera")))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1100, 800))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.render_window = None
        self.use_handles = os.environ.get("CYCLES_HOUDINI_PROFILE_HANDLES") == "1"
        self.display_mode = os.environ.get("CYCLES_HOUDINI_PROFILE_DISPLAY") or "normal"
        self.duration = float(os.environ.get("CYCLES_HOUDINI_PROFILE_SECONDS") or "20")
        self.operations = ("translate", "rotate")
        self.phase_count = 4 * len(self.operations)
        self.samplers = []
        self.prepared_phase = None
        self.panel = None
        self.phase = -1
        self.moving = False
        self.busy = False
        self.started = time.monotonic()
        self.timings = defaultdict(list)
        self.restores = []
        self.gaps = []
        self.report = dict(
            result="running",
            phases=[],
            source=str(self.source),
            source_sha256=self.digest,
            device=os.environ["CYCLES_HOUDINI_GUI_DEVICE"],
            runtime_root=os.environ["CYCLES_HOUDINI_HOST_ROOT"],
            overrides=self.overrides,
            config=native_scene.configuration(self.rop),
        )
        self.report["display_mode"] = self.display_mode
        self.report["seconds_per_phase"] = self.duration

        def wrap(owner, name):
            original = getattr(owner, name)

            def timed(*args, **kwargs):
                began = time.perf_counter()
                try:
                    if (
                        owner is RenderPanel
                        and name == "present_available"
                        and self.phase >= self.phase_count // 2
                        and self.display_mode == "no-presentation"
                    ):
                        panel = args[0]
                        if panel.entry:
                            generation, revision, owned = panel.entry.session.display_state(0)
                            if owned:
                                panel.entry.session.notify_presented(generation, revision)
                        return
                    return original(*args, **kwargs)
                finally:
                    if self.moving:
                        self.timings[owner.__name__ + "." + name].append(time.perf_counter() - began)

            setattr(owner, name, timed)
            self.restores.append((owner, name, original))

        for owner, name in (
            (registry.Entry, "preview_object_poses"),
            (RenderPanel, "poll"),
            (RenderPanel, "present_available"),
            (ImageCanvas, "paintEvent"),
            (viewer_display.gpu_canvas_class(), "paintEvent"),
            (light_viewer_state.LightState, "_refresh"),
            (light_viewer_state.LightState, "onStateToHandle"),
            (native_scene, "capture"),
            (native_scene.SceneObserver, "refresh"),
            (_native, "prepare_apply"),
            (_native, "finish_apply"),
        ):
            wrap(owner, name)
        self.timer = QtCore.QTimer()
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.edit)
        self.pulse = QtCore.QTimer()
        self.pulse.setTimerType(QtCore.Qt.PreciseTimer)
        self.pulse.setInterval(8)
        self.last_pulse = time.perf_counter()
        self.pulse.timeout.connect(self.beat)
        self.pulse.start()
        hou.ui.addEventLoopCallback(self.tick)

    def beat(self):
        now = time.perf_counter()
        if self.moving:
            self.gaps.append(now - self.last_pulse)
        self.last_pulse = now

    def edit(self):
        if not self.moving or self.busy:
            return
        self.busy = True
        try:
            elapsed = time.monotonic() - self.phase_started
            if elapsed >= self.duration:
                if self.use_handles:
                    QtTest.QTest.mouseRelease(
                        self.viewport_widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, self.last_point.toPoint()
                    )
                if self.profile:
                    self.profile.disable()
                self.moving = False
                self.timer.stop()
                delta = max(
                    abs(a - b)
                    for a, b in zip(
                        self.node.worldTransform().asTuple(), self.original_matrices[self.node.path()].asTuple()
                    )
                )
                assert delta > 0.01, "Native viewport drag did not move the selected object"
                profile_path = self.path.with_suffix(".phase" + str(self.phase) + ".pstats")
                top = []
                if self.profile:
                    self.profile.dump_stats(str(profile_path))
                    stats = pstats.Stats(self.profile)
                    top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:35]
                self.report["phases"].append(
                    dict(
                        phase=self.phase,
                        ipr=self.phase >= self.phase_count // 2,
                        node=self.node.path(),
                        operation=self.operation,
                        edits=self.edits,
                        heartbeat_p95_ms=float(np.quantile(self.gaps, 0.95)) * 1000,
                        heartbeat_max_ms=max(self.gaps) * 1000,
                        heartbeat_p99_ms=float(np.quantile(self.gaps, 0.99)) * 1000,
                        stalls_over_50ms=sum(gap > 0.05 for gap in self.gaps),
                        stalls_over_100ms=sum(gap > 0.1 for gap in self.gaps),
                        timings={
                            key: dict(
                                count=len(v),
                                total_ms=sum(v) * 1000,
                                max_ms=max(v) * 1000,
                                p95_ms=float(np.quantile(v, 0.95)) * 1000,
                            )
                            for key, v in self.timings.items()
                            if v
                        },
                        profile=[
                            dict(
                                file=k[0], line=k[1], name=k[2], calls=v[1], self_seconds=v[2], cumulative_seconds=v[3]
                            )
                            for k, v in top
                        ],
                        viewport_visible=self.viewport_window.isVisible(),
                        viewer_state=self.viewer.currentState(),
                        zoom_model_mutations=dict(self.zoom_mutations),
                        handle_drag=self.use_handles,
                        transform_delta=delta,
                        status=self.panel.entry.session.status(),
                        controller=self.panel.entry.session.diagnostics()["controller"],
                    )
                )
                for name, value in self.original_channels[self.node.path()].items():
                    self.node.parmTuple(name).set(value)
                self.next_at = time.monotonic() + 0.7
                self.save()
                return
            self.edits += 1
            began = time.perf_counter()
            if self.use_handles:
                point = self.start_point + self.drag_direction * (35 * (0.2 + math.sin(elapsed * 2)))
                event = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseMove,
                    point,
                    QtCore.QPointF(self.viewport_widget.mapToGlobal(point.toPoint())),
                    QtCore.Qt.NoButton,
                    QtCore.Qt.LeftButton,
                    QtCore.Qt.NoModifier,
                )
                QtWidgets.QApplication.sendEvent(self.viewport_widget, event)
                self.last_point = point
            else:
                if self.operation == "translate":
                    self.node.parm("tx").set(self.original[self.node.path()] + 0.5 * math.sin(elapsed * 3))
                else:
                    self.node.parm("ry").set(
                        self.original_channels[self.node.path()]["r"][1] + 45 * math.sin(elapsed * 2)
                    )
            self.timings["set_transform"].append(time.perf_counter() - began)
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 120 + self.phase_count * (self.duration + 3), (
                "Viewport profiling timed out"
            )
            if self.panel is None:
                panel = self.tab.activeInterfaceRootWidget()
                self.viewport_window = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
                self.render_window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
                if panel is None or self.viewport_window is None or self.render_window is None:
                    return
                self.panel = panel
                self.zoom_mutations = defaultdict(int)
                self.panel.zoom_menu.model().rowsInserted.connect(
                    lambda *args: self.zoom_mutations.__setitem__("insert", self.zoom_mutations["insert"] + 1)
                )
                self.panel.zoom_menu.model().rowsRemoved.connect(
                    lambda *args: self.zoom_mutations.__setitem__("remove", self.zoom_mutations["remove"] + 1)
                )
                screen = QtGui.QGuiApplication.primaryScreen().availableGeometry()
                half = screen.width() // 2
                self.viewport_window.setGeometry(screen.x(), screen.y() + 25, half - 8, min(900, screen.height() - 50))
                self.render_window.setGeometry(
                    screen.x() + half, screen.y() + 25, half - 8, min(900, screen.height() - 50)
                )
                self.viewport_window.show()
                self.render_window.show()
                if os.environ.get("CYCLES_HOUDINI_PROFILE_CPU_DISPLAY") == "1":
                    self.panel._display_queue.convert = _native.display_image
                self.panel.set_rop(self.rop)
                self.panel.render_button.click()
                self.save()
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if self.moving:
                return
            if self.phase < 0:
                frame = entry.session.frame_info()
                if not (
                    frame
                    and frame["complete"]
                    and status["state"] == "complete"
                    and self.panel._live_generation == frame["generation"]
                ):
                    return
                if os.environ.get("CYCLES_HOUDINI_PROFILE_GPU_PRIORITY") == "1":
                    import ctypes
                    from ctypes import wintypes

                    gdi = ctypes.WinDLL("gdi32")
                    get = gdi.D3DKMTGetProcessSchedulingPriorityClass
                    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
                    get.restype = ctypes.c_long
                    set_priority = gdi.D3DKMTSetProcessSchedulingPriorityClass
                    set_priority.argtypes = [wintypes.HANDLE, ctypes.c_int]
                    set_priority.restype = ctypes.c_long
                    process = entry.session._controller.backend.process
                    before = ctypes.c_int()
                    after = ctypes.c_int()
                    handle = wintypes.HANDLE(int(process._handle))
                    old_status = get(handle, ctypes.byref(before))
                    changed = set_priority(handle, 1)
                    new_status = get(handle, ctypes.byref(after))
                    self.report["gpu_priority"] = dict(
                        worker_pid=process.pid,
                        before=before.value,
                        after=after.value,
                        query_status=old_status,
                        set_status=changed,
                        verify_status=new_status,
                    )
                self.panel.cancel()
                self.next_at = time.monotonic() + 0.5
                self.phase = 0
            if time.monotonic() < self.next_at:
                return
            if len(self.report["phases"]) > self.phase:
                self.phase += 1
                if self.phase == self.phase_count:
                    self.finish()
                    return
                if self.phase == self.phase_count // 2:
                    self.panel.render()
                    self.next_at = time.monotonic() + 1
                    return
            if self.prepared_phase != self.phase:
                self.node = (self.actor, self.light)[self.phase % 2]
                self.node.setCurrent(True, clear_all_selected=True)
                self.viewer.setIsCurrentTab()
                state = (
                    light_viewer_state.STATE_NAME
                    if self.phase % 2 and light_properties.is_cycles_light(self.node)
                    else "pose"
                )
                self.viewer.setCurrentState(state, generate=hou.stateGenerateMode.Enter)
                self.prepared_phase = self.phase
                self.next_at = time.monotonic() + 1
                return
            self.node = (self.actor, self.light)[self.phase % 2]
            self.operation = self.operations[(self.phase // 2) % len(self.operations)]
            state = (
                light_viewer_state.STATE_NAME
                if self.phase % 2 and light_properties.is_cycles_light(self.node)
                else "pose"
            )
            assert self.viewport_window.isVisible() and self.panel.canvas.isVisible()
            if self.phase == self.phase_count // 2 and self.display_mode == "no-presentation":
                # Diagnostic control only: preserve engine updates/transport,
                # acknowledge owned frames but skip display conversion/paint.
                self.panel._display_queue.follow(None, 0, {}, _native.frame_info)
            if self.use_handles:
                self.viewer.curViewport().setCamera(None)
                self.viewer.curViewport().changeType(hou.geometryViewportType.Front)
                center = self.node.worldTransform().extractTranslates()
                self.viewer.curViewport().frameBoundingBox(
                    hou.BoundingBox(
                        center[0] - 4, center[1] - 3, center[2] - 4, center[0] + 4, center[1] + 3, center[2] + 4
                    )
                )
                self.viewport_widget = next(
                    w
                    for w in self.viewport_window.findChildren(QtWidgets.QWidget)
                    if w.objectName() == "RE_Window" and w.width() == self.viewer.curViewport().size()[2]
                )
                axis = (
                    hou.Vector3(1, 0, 0) * hou.Matrix3(self.node.worldTransform())
                    if self.phase % 2
                    else hou.Vector3(1, 0, 0)
                )
                point = self.viewer.curViewport().mapToScreen(center + axis.normalized())
                origin = self.viewer.curViewport().mapToScreen(center)
                direction = QtCore.QPointF(point[0] - origin[0], origin[1] - point[1])
                length = math.hypot(direction.x(), direction.y())
                assert length > 1
                self.drag_direction = direction / length
                self.start_point = (
                    QtCore.QPointF(origin[0], self.viewport_widget.height() - 1 - origin[1]) + self.drag_direction * 60
                )
                self.last_point = self.start_point
                self.viewport_window.raise_()
                self.viewport_window.activateWindow()
                self.viewport_widget.setFocus()
                QtGui.QCursor.setPos(self.viewport_widget.mapToGlobal(self.start_point.toPoint()))
                if state == "pose":
                    QtTest.QTest.keyClick(self.viewport_widget, QtCore.Qt.Key_T)
                    QtTest.QTest.keyClick(self.viewport_widget, QtCore.Qt.Key_Return)
                QtTest.QTest.mouseMove(self.viewport_widget, self.start_point.toPoint())
                QtTest.QTest.qWait(120)
                QtTest.QTest.mousePress(
                    self.viewport_widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, self.start_point.toPoint()
                )
            self.timings.clear()
            self.gaps = []
            self.edits = 0
            self.phase_started = time.monotonic()
            self.zoom_mutations.clear()
            self.profile = cProfile.Profile() if os.environ.get("CYCLES_HOUDINI_PROFILE_CPROFILE") == "1" else None
            if self.profile:
                self.profile.enable()
            if os.environ.get("CYCLES_HOUDINI_PROFILE_NATIVE_STACKS") == "1":
                executable = runtime_executable("ch_gui_debugger.exe")
                self.samplers.append(
                    subprocess.Popen(
                        [
                            str(executable),
                            "--sample",
                            str(os.getpid()),
                            str(threading.get_native_id()),
                            str(self.path.with_suffix(".phase" + str(self.phase) + ".ui-stacks.log")),
                            "4000",
                        ],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                )
            self.last_pulse = time.perf_counter()
            self.moving = True
            self.timer.start()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if getattr(self, "finishing", False):
            return
        self.finishing = True
        self.timer.stop()
        self.pulse.stop()
        self.moving = False
        if getattr(self, "profile", None):
            self.profile.disable()
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        self.finish_error = error
        if error and hasattr(self, "node"):
            self.report["failure_context"] = dict(
                node=self.node.path(),
                node_type=self.node.type().name(),
                state=self.viewer.currentState(),
                selection=[n.path() for n in hou.selectedNodes()],
            )
        hou.ui.postEventCallback(self.finish_host)

    def finish_host(self):
        error = self.finish_error
        for sampler in self.samplers:
            assert sampler.wait(timeout=10) == 0, "UI stack sampler failed"
        self.viewer.setCurrentState("select")
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        for owner, name, original in reversed(self.restores):
            setattr(owner, name, original)
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.digest
        self.report.update(
            result="failed" if error else "passed", error=error, elapsed_seconds=time.monotonic() - self.started
        )
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
