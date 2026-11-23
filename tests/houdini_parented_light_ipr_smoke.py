# SPDX-License-Identifier: Apache-2.0
"""Parent a showcase light, rotate its Null, drag it, and verify live IPR.

The native light, dynamic shape handles and Cycles must agree on world space.
The saved showcase stays unchanged on disk. One Cycles viewer is used.
"""

from support.paths import artifacts_root

from native_test_runtime import build_manifest
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import (
    device_selection,
    devices,
    light_viewer_state,
    light_properties,
    matrix_from_houdini,
    object_scene,
    registry,
)


class ParentedLightWorkflow:
    def __init__(self):
        self.root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[], handle_errors=[], host_pid=os.getpid())
        self.report["test_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.report["manifest_sha256"] = hashlib.sha256((build_manifest()).read_bytes()).hexdigest()
        self.began = time.monotonic()
        self.busy = False
        self.stage = "initial"
        self.at = 0
        self.generation = 0
        self.states = []
        self.values = {}
        self.pose_mismatches = []
        self.index = 0
        self.drag_index = 0
        self.native_drag_index = 0
        self.dragging = False
        self.staging = None
        self.staging_stream = None
        self.staging_runs = []
        self.restage = os.environ.get("CYCLES_HOUDINI_TEST_LIGHT_RESTAGE") == "1"
        self.report["drags_during_staging"] = 0
        self.report["refreshes_during_drag"] = 0
        self.original_init = light_viewer_state.LightState.__init__
        self.original_values = light_viewer_state.LightState.onStateToHandle
        self.original_state = light_viewer_state.LightState.onHandleToState
        self.original_refresh = light_viewer_state.LightState._refresh

        def capture_init(state, *args, **kwargs):
            self.original_init(state, *args, **kwargs)
            self.states.append(state)

        def capture_values(state, kwargs):
            try:
                self.original_values(state, kwargs)
                self.values[kwargs["handle"]] = dict(kwargs["parms"])
            except Exception:
                self.report["handle_errors"].append(traceback.format_exc())
                self.save()
                raise

        def capture_state(state, kwargs):
            self.report["events"].append(
                dict(name="native-handle-edit", handle=kwargs["handle"], modified=list(kwargs["mod_parms"]))
            )
            self.original_state(state, kwargs)

        def capture_refresh(state, *args, **kwargs):
            try:
                if self.dragging:
                    self.report["refreshes_during_drag"] += 1
                return self.original_refresh(state, *args, **kwargs)
            except Exception:
                self.report["handle_errors"].append(traceback.format_exc())
                self.save()
                raise

        light_viewer_state.LightState.__init__ = capture_init
        light_viewer_state.LightState.onStateToHandle = capture_values
        light_viewer_state.LightState.onHandleToState = capture_state
        light_viewer_state.LightState._refresh = capture_refresh

        example = artifacts_root() / "examples/sop-fx-courtyard"
        scene = example / "sop-fx-courtyard-adaptive.hiplc"
        self.report["scene_sha256"] = hashlib.sha256(scene.read_bytes()).hexdigest()
        hou.hipFile.load(str(scene), suppress_save_prompt=True)
        hou.putenv("OCIO", str(example / "showcase.ocio"))
        hou.Color.reloadOCIO()
        self.rop = hou.node("/out/cycles_sop_courtyard")
        lights = [
            n for n in object_scene.selected(self.rop, hou.time(), lights=True) if light_properties.is_cycles_light(n)
        ]
        assert len(lights) == 1, lights
        self.light = lights[0]
        # The current showcase key is a point. This disposable handle fixture
        # explicitly exercises an area; it does not rely on old saved settings.
        self.light.setParms(
            dict(cycles_light_type="grid", cycles_light_area_shape="rectangle", singlesided=1, reverse=0)
        )
        self.light.setParms(dict(areasizex=2, areasizey=1))
        position = self.light.worldTransform().extractTranslates()
        back = (position - hou.Vector3(0, 1, 0)).normalized()
        right = hou.Vector3(0, 1, 0).cross(back).normalized()
        up = back.cross(right)
        self.light.setWorldTransform(hou.Matrix4((*right, 0, *up, 0, *back, 0, *position, 1)))
        self.shape_handle = light_viewer_state.RECTANGLE
        self.report["light_settings"] = dict(
            kind=light_properties.light_type(self.light),
            shape=self.light.evalParm("cycles_light_area_shape"),
            handle=self.shape_handle,
        )
        self.cleaned_transform = os.environ.get("CYCLES_HOUDINI_TEST_LIGHT_PRETRANSFORM") == "1"
        self.report["cleaned_transform"] = self.cleaned_transform
        if self.cleaned_transform:
            self.light.moveParmTransformIntoPreTransform()
        self.rop = hou.node("/out/cycles_sop_courtyard")
        self.report["source_resolution"] = {
            name: self.rop.evalParm(name) for name in ("resx", "resy", "resolution_percent", "ipr_resolution_percent")
        }
        self.width = int(os.environ.get("CYCLES_HOUDINI_TEST_LIGHT_WIDTH", "480"))
        self.height = int(os.environ.get("CYCLES_HOUDINI_TEST_LIGHT_HEIGHT", "270"))
        samples = int(os.environ.get("CYCLES_HOUDINI_TEST_LIGHT_SAMPLES", "16"))
        self.report["render_settings"] = dict(width=self.width, height=self.height, samples=samples)
        requested = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        self.report["saved_device_ids"] = device_selection.selected_devices(self.rop, hou.time())
        ids = (requested,)
        if os.environ.get("CYCLES_HOUDINI_TEST_LIGHT_MIXED") == "1":
            backend = next(d["backend"] for d in devices() if d["id"] == requested)
            assert backend in ("CUDA", "OPTIX")
            ids = tuple(sorted(d["id"] for d in devices() if d["backend"] == backend))
            assert len(ids) > 1
        self.device_ids = device_selection.set_devices(self.rop, ids)
        self.report["expected_device_ids"] = self.device_ids
        self.rop.setParms(
            dict(
                resx=self.width,
                resy=self.height,
                samples=samples,
                ipr_samples=samples,
                resolution_percent=100,
                ipr_resolution_percent=100,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                allowmotionblur=0,
                viewer_interactive=1,
                world_source="color",
                lights=self.light.path(),
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.original_light_t = self.light.parmTuple("t").eval()
        self.parent = hou.node("/obj").createNode("null", "LIGHT_PARENT")
        self.other_parent = hou.node("/obj").createNode("null", "OTHER_PARENT")
        self.other_parent.parm("tx").set(-1)
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.viewer.setPwd(hou.node("/obj"))
        self.window = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
        self.window.show()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.panel.set_rop(self.rop)
        self.last_paint = {}
        self.paint_count = 0
        self.original_paint = type(self.panel.canvas).paintEvent

        def painted(canvas, event):
            self.original_paint(canvas, event)
            if canvas is self.panel.canvas and not canvas.image.isNull():
                self.last_paint = dict(canvas.frame_metadata)
                self.paint_count += 1

        type(self.panel.canvas).paintEvent = painted
        # Houdini restores floating-panel geometry after its initial show.
        # Arrange the actual panel windows at idle, after that restore.
        self.stage = "arrange"
        self.at = time.monotonic() + 0.7
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)
        self.save()

    def save(self):
        self.report["stage"] = self.stage
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    @staticmethod
    def rect(value):
        return [value.x(), value.y(), value.width(), value.height()]

    def arrange(self):
        self.panel_window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
        screens = QtGui.QGuiApplication.screens()
        screen = max(screens, key=lambda item: item.availableGeometry().width())
        area = screen.availableGeometry()
        self.report["screens"] = [
            dict(name=item.name(), available=self.rect(item.availableGeometry()), dpr=item.devicePixelRatio())
            for item in screens
        ]
        width = (area.width() - 90) // 2
        height = min(1000, area.height() - 120)
        if width >= 650:
            left = QtCore.QRect(area.x() + 15, area.y() + 50, width, height)
            right = QtCore.QRect(left.right() + 35, left.y(), width, height)
        else:
            height = (area.height() - 140) // 2
            left = QtCore.QRect(area.x() + 20, area.y() + 40, area.width() - 60, height)
            right = QtCore.QRect(left.x(), left.bottom() + 45, left.width(), height)
        self.window.setGeometry(left)
        self.panel_window.setGeometry(right)
        self.window.show()
        self.panel_window.show()
        self.panel_window.raise_()
        self.window.raise_()
        self.report["placement_requested"] = dict(viewport=self.rect(left), cycles=self.rect(right))

    def runtime_snapshot(self):
        host = Path(hou.getenv("CYCLES_HOUDINI_HOST_ROOT"))
        return {
            p.relative_to(host).as_posix(): dict(
                sha256=hashlib.sha256(p.read_bytes()).hexdigest(), mtime_ns=p.stat().st_mtime_ns
            )
            for p in host.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")
        }

    def poll_staging(self):
        if self.staging is None or self.staging.poll() is None:
            return
        code = self.staging.returncode
        self.staging_stream.close()
        self.staging = None
        assert self.runtime_snapshot() == self.before_staging, "Restaging modified a loaded runtime"
        assert code == 0, ("Restaging failed", code)
        self.staging_runs.append(
            dict(exit_code=code, frozen_files=len(self.before_staging), bytes_and_modification_times_preserved=True)
        )
        self.report["staging_runs"] = self.staging_runs

    def start_staging(self):
        self.poll_staging()
        if not self.restage or self.staging is not None or len(self.staging_runs) >= 2:
            return
        self.before_staging = self.runtime_snapshot()
        self.staging_stream = self.path.with_suffix(".stage" + str(len(self.staging_runs)) + ".log").open(
            "w", encoding="utf-8"
        )
        environment = os.environ.copy()
        # Reuse the build's recorded color configuration so this dedicated
        # restaging check also leaves the public manifest identity unchanged.
        manifest = json.loads((build_manifest()).read_text(encoding="utf-8-sig"))
        config = manifest["ocio"].get("config")
        if config:
            environment["OCIO"] = config
        else:
            environment.pop("OCIO", None)
        shell = shutil.which("pwsh.exe")
        assert shell, "The development staging script requires PowerShell 7"
        self.staging = subprocess.Popen(
            [
                shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.root / "scripts/stage-development.ps1"),
            ],
            cwd=self.root,
            env=environment,
            stdout=self.staging_stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def ready(self):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        assert not entry.error, entry.error
        assert status["state"] != "failed", status
        info = entry.session.frame_info()
        metadata = getattr(self.panel.canvas, "frame_metadata", {})
        if not (
            info
            and info["complete"]
            and info["current_scene"]
            and info["generation"] > self.generation
            and info["generation"] == status["generation"]
            and not status.get("pending_update")
            and not status.get("capture_pending")
            and entry.applied_serial == entry.change_serial
            and metadata.get("generation") == info["generation"]
            and info["rendered_width"] == self.width
            and info["rendered_height"] == self.height
            and metadata.get("rendered_width") == self.width
            and metadata.get("rendered_height") == self.height
            and metadata.get("render_revision") == status.get("render_revision")
            and self.last_paint == metadata
        ):
            return False
        assert tuple(status["render_device_ids"]) == self.device_ids, status["render_device_ids"]
        viewport_rect = self.window.frameGeometry()
        cycles_rect = self.panel_window.frameGeometry()
        assert not viewport_rect.intersects(cycles_rect), (self.rect(viewport_rect), self.rect(cycles_rect))
        assert self.window.isVisible() and self.panel.canvas.isVisible()
        self.generation = info["generation"]
        pixels = entry.session.frame()["planes"]["combined"]["pixels"]
        current = np.frombuffer(pixels, np.float32).copy()
        delta = float(np.mean(np.abs(current - self.image))) if hasattr(self, "image") else None
        self.image = current
        self.report["events"].append(
            dict(
                name="rendered-image",
                stage=self.stage,
                generation=self.generation,
                image_delta=delta,
                frame_info=info,
                status=status,
                painted_metadata=self.last_paint,
                paint_count=self.paint_count,
                viewport_rect=self.rect(viewport_rect),
                cycles_rect=self.rect(cycles_rect),
            )
        )
        if self.stage != "initial":
            assert delta is not None and delta > 1e-6, ("Light edit did not affect the image", self.stage, delta)
        return True

    def check_pose(self, name):
        expected = self.light.worldTransform()
        native_pose = matrix_from_houdini(expected)
        definitions = object_scene.light_definitions(self.light, hou.time(), [])
        assert len(definitions) == 1, definitions
        render_pose = definitions[0]["transform"]
        delta = max(abs(a - b) for a, b in zip(native_pose, render_pose))
        self.report["events"].append(
            dict(
                name=name,
                native_pose=native_pose,
                render_pose=render_pose,
                max_matrix_error=delta,
                local_translation=list(self.light.parmTuple("t").eval()),
            )
        )
        if delta > 1e-7:
            self.pose_mismatches.append(dict(name=name, error=delta))
        if any(state.active and state.node == self.light for state in self.states):
            state = next(state for state in self.states if state.active and state.node == self.light)
            assert state.handles[self.shape_handle].isEnabled()
            assert all(
                not state.handles[name].isEnabled() for name in light_viewer_state.SHAPES if name != self.shape_handle
            )
            values = self.values[self.shape_handle]
            handle_center = (
                hou.Vector3(tuple(values["center" + axis] for axis in "xyz"))
                * self.light.preTransform()
                * self.light.parentAndSubnetTransform()
            )
            error = (handle_center - expected.extractTranslates()).length()
            self.report["events"][-1]["handle_world_position_error"] = error
            assert error < 1e-5, ("Shape handle applied the parent transform incorrectly", name, error)

    def activate(self):
        self.light.setCurrent(True, clear_all_selected=True)
        self.viewer.setPwd(hou.node("/obj"))
        self.window.raise_()
        self.window.activateWindow()
        center = self.light.worldTransform().extractTranslates()
        self.viewer.curViewport().changeType(hou.geometryViewportType.Front)
        self.viewer.curViewport().frameBoundingBox(
            hou.BoundingBox(center[0] - 5, center[1] - 4, center[2] - 4, center[0] + 5, center[1] + 4, center[2] + 4)
        )
        self.viewer.setCurrentState(light_viewer_state.STATE_NAME, generate=hou.stateGenerateMode.Enter)

    def drag(self):
        self.start_staging()
        viewport = self.viewer.curViewport()
        widget = next(
            w
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window" and w.width() == viewport.size()[2]
        )
        center = self.light.worldTransform().extractTranslates()

        def pixel(world):
            p = viewport.mapToScreen(world)
            return QtCore.QPointF(p[0], widget.height() - 1 - p[1])

        axis = hou.Vector3(1, 0, 0) * hou.Matrix3(self.light.worldTransform())
        axis = axis.normalized()
        origin = pixel(center)
        direction = pixel(center + axis) - origin
        length = (direction.x() ** 2 + direction.y() ** 2) ** 0.5
        assert length > 1, "Light X axis is edge-on to the test viewport"
        direction /= length
        start = origin + direction * 60
        end = origin + direction * 110
        self.before_drag = self.light.worldTransform()
        self.report["drag_values"] = dict(self.values)
        self.window.screen().grabWindow(self.window.winId()).save(str(self.path.with_suffix(".before-drag.png")))
        QtGui.QCursor.setPos(widget.mapToGlobal(start.toPoint()))
        QtTest.QTest.mouseMove(widget, start.toPoint())
        QtTest.QTest.qWait(100)
        self.dragging = True
        if self.staging is not None and self.staging.poll() is None:
            self.report["drags_during_staging"] += 1
        QtTest.QTest.mousePress(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start.toPoint())
        for step in range(1, 13):
            point = start + (end - start) * (step / 12)
            event = QtGui.QMouseEvent(
                QtCore.QEvent.MouseMove,
                point,
                QtCore.QPointF(widget.mapToGlobal(point.toPoint())),
                QtCore.Qt.NoButton,
                QtCore.Qt.LeftButton,
                QtCore.Qt.NoModifier,
            )
            QtWidgets.QApplication.sendEvent(widget, event)
            QtTest.QTest.qWait(30)
        QtTest.QTest.mouseRelease(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, end.toPoint())
        self.dragging = False

    def resize(self):
        viewport = self.viewer.curViewport()
        widget = next(
            w
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window" and w.width() == viewport.size()[2]
        )
        matrix = self.light.worldTransform()
        center = matrix.extractTranslates()
        axis = hou.Vector3(1, 0, 0) * hou.Matrix3(matrix)
        half_width = self.light.evalParm("areasizex") * 0.5

        def pixel(world):
            p = viewport.mapToScreen(world)
            return QtCore.QPointF(p[0], widget.height() - 1 - p[1])

        start = pixel(center + axis * half_width)
        end = pixel(center + axis * (half_width + 0.45))
        self.before_resize = light_properties.area_size(self.light)
        self.before_resize_world = self.light.worldTransform()
        self.resize_callback_start = len(self.report["events"])
        QtGui.QCursor.setPos(widget.mapToGlobal(start.toPoint()))
        QtTest.QTest.mouseMove(widget, start.toPoint())
        QtTest.QTest.qWait(100)
        self.dragging = True
        QtTest.QTest.mousePress(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start.toPoint())
        for step in range(1, 13):
            point = start + (end - start) * (step / 12)
            event = QtGui.QMouseEvent(
                QtCore.QEvent.MouseMove,
                point,
                QtCore.QPointF(widget.mapToGlobal(point.toPoint())),
                QtCore.Qt.NoButton,
                QtCore.Qt.LeftButton,
                QtCore.Qt.NoModifier,
            )
            QtWidgets.QApplication.sendEvent(widget, event)
            QtTest.QTest.qWait(30)
        QtTest.QTest.mouseRelease(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, end.toPoint())
        self.dragging = False

    def native_drag(self):
        viewport = self.viewer.curViewport()
        widget = next(
            w
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window" and w.width() == viewport.size()[2]
        )
        center = self.light.worldTransform().extractTranslates()

        def pixel(world):
            p = viewport.mapToScreen(world)
            return QtCore.QPoint(round(p[0]), widget.height() - 1 - round(p[1]))

        start = pixel(center + hou.Vector3(1, 0, 0))
        end = pixel(center + hou.Vector3(1.8, 0, 0))
        self.before_drag = self.light.worldTransform()
        self.report["native_selection"] = [node.path() for node in hou.selectedNodes()]
        self.window.screen().grabWindow(self.window.winId()).save(str(self.path.with_suffix(".native-move.png")))
        if self.viewer.currentState() == light_viewer_state.STATE_NAME:
            # Houdini can hand a picked HDA to its declared default state after
            # Pose's selection is confirmed. Exercise the actual resulting tool.
            self.drag()
            return
        QtGui.QCursor.setPos(widget.mapToGlobal(start))
        QtTest.QTest.mouseMove(widget, start)
        QtTest.QTest.qWait(100)
        QtTest.QTest.mousePress(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start)
        for step in range(1, 13):
            point = QtCore.QPointF(start) + QtCore.QPointF(end - start) * (step / 12)
            event = QtGui.QMouseEvent(
                QtCore.QEvent.MouseMove,
                point,
                QtCore.QPointF(widget.mapToGlobal(point.toPoint())),
                QtCore.Qt.NoButton,
                QtCore.Qt.LeftButton,
                QtCore.Qt.NoModifier,
            )
            QtWidgets.QApplication.sendEvent(widget, event)
            QtTest.QTest.qWait(30)
        QtTest.QTest.mouseRelease(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, end)

    def tick(self):
        if self.busy or time.monotonic() < self.at:
            return
        self.busy = True
        try:
            self.poll_staging()
            assert time.monotonic() - self.began < 130, self.stage
            if self.stage == "arrange":
                self.arrange()
                self.panel.auto.setChecked(True)
                self.panel.render_button.click()
                self.stage = "initial"
                self.at = time.monotonic() + 0.8
            elif self.stage == "initial":
                if not self.ready():
                    return
                self.worker = self.panel.entry.session.status()["renderer_pid"]
                self.conversions = self.panel.entry.session.status()["geometry_conversions"]
                self.light.setInput(0, self.parent)
                self.parent.parm("ry").set(35)
                self.stage = "parent-rotated"
            elif self.stage == "parent-rotated":
                if not self.ready():
                    return
                self.check_pose("Parent rotation moves the native light")
                self.activate()
                self.stage = "drag-ready"
                self.at = time.monotonic() + 0.5
            elif self.stage == "drag-ready":
                active = [s for s in self.states if s.active and s.node == self.light]
                assert len(active) == 1, active
                self.drag()
                self.stage = "dragged"
            elif self.stage == "dragged":
                assert self.before_drag != self.light.worldTransform(), (
                    "Native handle drag did not move the parented light"
                )
                if not self.ready():
                    return
                self.check_pose("Parented light drag agrees with render")
                self.window.screen().grabWindow(self.window.winId()).save(str(self.path.with_suffix(".parented.png")))
                self.stage = "resize-ready"
                self.at = time.monotonic() + 0.3
            elif self.stage == "resize-ready":
                self.resize()
                self.stage = "resized"
            elif self.stage == "resized":
                assert light_properties.area_size(self.light) != self.before_resize, (
                    "Native parented area-size handle did not resize the light"
                )
                assert (
                    max(
                        abs(a - b)
                        for a, b in zip(self.light.worldTransform().asTuple(), self.before_resize_world.asTuple())
                    )
                    < 1e-6
                )
                assert any(
                    event.get("handle") == self.shape_handle
                    for event in self.report["events"][self.resize_callback_start :]
                )
                if not self.ready():
                    return
                self.check_pose("Parented size handle stays on its emitter")
                self.pre_clean_world = self.light.worldTransform()
                self.light.moveParmTransformIntoPreTransform()
                self.stage = "live-clean-ready"
                self.at = time.monotonic() + 0.4
            elif self.stage == "live-clean-ready":
                assert (
                    max(
                        abs(a - b)
                        for a, b in zip(self.light.worldTransform().asTuple(), self.pre_clean_world.asTuple())
                    )
                    < 1e-6
                )
                self.check_pose("Live Clean Transform preserves the native handle frame")
                self.resize()
                self.stage = "live-clean-resized"
            elif self.stage == "live-clean-resized":
                assert light_properties.area_size(self.light) != self.before_resize, (
                    "Clean Transform left a stale size handle"
                )
                assert (
                    max(
                        abs(a - b)
                        for a, b in zip(self.light.worldTransform().asTuple(), self.before_resize_world.asTuple())
                    )
                    < 1e-6
                )
                assert any(
                    event.get("handle") == self.shape_handle
                    for event in self.report["events"][self.resize_callback_start :]
                )
                if not self.ready():
                    return
                self.check_pose("Size handle remains usable after live Clean Transform")
                self.parent.parm("ry").set(-20)
                self.stage = "parent-rotated-again"
            elif self.stage == "parent-rotated-again":
                if not self.ready():
                    return
                self.check_pose("Parent rotation while light handles are active")
                self.light.setInput(0, self.other_parent)
                self.stage = "reparented"
            elif self.stage == "reparented":
                if not self.ready():
                    return
                self.check_pose("Reparent while IPR and light handles are active")
                self.light.setInput(0, None)
                self.stage = "unparented"
            elif self.stage == "unparented":
                if not self.ready():
                    return
                self.check_pose("Disconnect parent while IPR and light handles are active")
                self.light.setInput(0, self.parent)
                self.parent.parm("ry").set(10)
                self.stage = "parent-sweep"
            elif self.stage == "parent-sweep":
                if not self.ready():
                    return
                self.check_pose("Parent rotation step " + str(self.index))
                self.index += 1
                if self.index < 5:
                    self.parent.parm("ry").set(10 + 10 * self.index)
                else:
                    self.activate()
                    self.stage = "stress-drag-ready"
                    self.at = time.monotonic() + 0.25
            elif self.stage == "stress-drag-ready":
                self.drag()
                self.stage = "stress-dragged"
            elif self.stage == "stress-dragged":
                assert self.before_drag != self.light.worldTransform(), "Repeated handle drag did not move the light"
                if not self.ready():
                    return
                self.check_pose("Repeated live handle drag " + str(self.drag_index))
                self.drag_index += 1
                if self.drag_index < 8:
                    self.activate()
                    self.stage = "stress-drag-ready"
                    self.at = time.monotonic() + 0.25
                else:
                    self.report["completed_viewport_drags"] = self.drag_index + 1
                    self.viewer.enterTranslateToolState()
                    self.stage = "native-pick"
                    self.at = time.monotonic() + 0.3
            elif self.stage == "native-pick":
                widget = next(
                    w
                    for w in self.window.findChildren(QtWidgets.QWidget)
                    if w.objectName() == "RE_Window" and w.width() == self.viewer.curViewport().size()[2]
                )
                p = self.viewer.curViewport().mapToScreen(self.light.worldTransform().extractTranslates())
                point = QtCore.QPoint(round(p[0]), widget.height() - 1 - round(p[1]))
                self.window.raise_()
                self.window.activateWindow()
                widget.setFocus()
                QtGui.QCursor.setPos(widget.mapToGlobal(point))
                QtTest.QTest.mouseMove(widget, point)
                QtTest.QTest.qWait(100)
                QtTest.QTest.mouseClick(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
                self.stage = "native-confirm"
                self.at = time.monotonic() + 0.3
            elif self.stage == "native-confirm":
                widget = next(
                    w
                    for w in self.window.findChildren(QtWidgets.QWidget)
                    if w.objectName() == "RE_Window" and w.width() == self.viewer.curViewport().size()[2]
                )
                self.window.raise_()
                self.window.activateWindow()
                widget.setFocus()
                QtGui.QCursor.setPos(widget.mapToGlobal(widget.rect().center()))
                QtTest.QTest.qWait(100)
                assert self.light in hou.selectedNodes(), [node.path() for node in hou.selectedNodes()]
                # Pose starts its own object-selection transaction on entering
                # the Move tool; confirm the viewport pick as an artist does.
                QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Return)
                self.stage = "native-ready"
                self.at = time.monotonic() + 0.3
            elif self.stage == "native-ready":
                self.report["native_move_state"] = self.viewer.currentState()
                self.native_drag()
                self.stage = "native-dragged"
            elif self.stage == "native-dragged":
                assert self.before_drag != self.light.worldTransform(), (
                    "Native Houdini Move tool did not move the parented light"
                )
                if not self.ready():
                    return
                self.check_pose("Native Houdini Move tool " + str(self.native_drag_index))
                self.native_drag_index += 1
                if self.native_drag_index < 3:
                    self.stage = "native-ready"
                    self.at = time.monotonic() + 0.2
                else:
                    self.report["completed_native_move_drags"] = self.native_drag_index
                    assert not self.pose_mismatches, self.pose_mismatches
                    assert not self.report["handle_errors"], self.report["handle_errors"]
                    assert self.panel.entry.session.status()["renderer_pid"] == self.worker
                    assert self.panel.entry.session.status()["geometry_conversions"] == self.conversions
                    self.finish()
                    return
            self.save()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.staging is not None:
            self.staging.wait(timeout=20)
            try:
                self.poll_staging()
            except Exception:
                error = error or traceback.format_exc()
        if self.restage and not error:
            if len(self.staging_runs) != 2 or not self.report["drags_during_staging"]:
                error = "Restaging concurrency was not exercised twice during the live workflow"
        self.panel.grab().save(str(self.path.with_suffix(".cycles.png")))
        self.report.update(
            result="failed" if error else "passed",
            error=error,
            pose_mismatches=self.pose_mismatches,
            wall_seconds=time.monotonic() - self.began,
        )
        self.save()
        hou.ui.removeEventLoopCallback(self.callback)
        self.viewer.enterViewState()
        self.panel.dispose()
        registry.close_all()
        type(self.panel.canvas).paintEvent = self.original_paint
        light_viewer_state.LightState.__init__ = self.original_init
        light_viewer_state.LightState.onStateToHandle = self.original_values
        light_viewer_state.LightState.onHandleToState = self.original_state
        light_viewer_state.LightState._refresh = self.original_refresh
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = ParentedLightWorkflow()
