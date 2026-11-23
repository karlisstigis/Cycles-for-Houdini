# SPDX-License-Identifier: Apache-2.0
"""Native mouse drags, with a visible viewport beside one Cycles viewer.

This measures synthetic input delivery, host parameter effects and Cycles canvas
paint completion. Qt event-loop acknowledgements are not GPU/display timings.
Screenshots are taken outside the measurement windows. Packed geometry uses an
explicit viewport bounding-box LOD; the renderer retains the full prototype.
"""

from support.paths import artifacts_root

from native_test_runtime import build_manifest
import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import threading
import time
import traceback

import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import _native, device_selection, registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_NO_MOTION"] = "1"
os.environ.setdefault("CYCLES_HOUDINI_BENCH_PACKED_COUNT", "1000")
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
fixture = runpy.run_path(
    str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="viewport_drag_fixture"
)
HeavyResponse = fixture["HeavyResponse"]
PackedResponse = fixture["PackedResponse"]


class InputObserver(QtCore.QObject):
    def __init__(self, workflow):
        super().__init__()
        self.workflow = workflow

    def eventFilter(self, widget, event):
        workflow = self.workflow
        if widget is not workflow.widget:
            return False
        if event.type() == QtCore.QEvent.MouseMove and workflow.case:
            record = workflow.input_events.get(event.timestamp())
            if record is not None:
                record["delivered"] = time.monotonic()
                workflow.current_input = record["index"]
                QtCore.QTimer.singleShot(0, lambda r=record: r.setdefault("event_loop_return", time.monotonic()))
            elif workflow.active_drag and len(workflow.case.setdefault("unmatched_inputs", [])) < 24:
                workflow.case["unmatched_inputs"].append(
                    dict(
                        at=time.monotonic(),
                        stamp=event.timestamp(),
                        position=[event.position().x(), event.position().y()],
                    )
                )
        return False


class ViewportDrag(HeavyResponse):
    create_render_panel = False

    def __init__(self):
        self.input_events = {}
        self.current_input = None
        self.active_drag = False
        self.producer = None
        self.producer_stop = threading.Event()
        self.geometry_entered = threading.Event()
        self.geometry_release = threading.Event()
        self.prewarm_original = _native.prewarm_apply
        self.bursts = int(os.environ.get("CYCLES_HOUDINI_VIEWPORT_BURSTS", "2"))
        self.moves = int(os.environ.get("CYCLES_HOUDINI_VIEWPORT_MOVES", "24"))
        self.interval = float(os.environ.get("CYCLES_HOUDINI_VIEWPORT_INTERVAL", ".04"))
        self.run_mode = "viewport-only"
        self.cases = []
        self.stage = "arrange"
        self.paints = []
        self.all_paints = []
        self.last_paint = {}
        self.submissions = {}
        self.effects = []
        self.screenshots = []
        self.test_index = 0
        super().__init__()
        self.report["scope"] = (
            "Real mouse events sent to the native Houdini viewport, beside one Cycles viewer. "
            "No HOM camera/object writes occur during a drag. Input events are synthetic; "
            "event-loop return does not measure viewport GPU completion or physical display latency."
        )
        self.report["viewport_lod"] = "Bounding Box for packed copies; full courtyard geometry"
        self.report["packed_fixture"] = dict(
            instances=self.instance_count,
            prototype_triangles=2 * (self.prototype_rows - 1) ** 2,
            renderer_lod="Full Geometry",
        )
        manifest = build_manifest()
        self.report["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.report["test_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.report["assets"] = [
            dict(path=str(path.relative_to(root)), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            for path in (
                artifacts_root() / "examples/sop-fx-courtyard/sop-fx-courtyard-current.hiplc",
                artifacts_root() / "examples/sop-fx-courtyard/showcase.ocio",
            )
        ]
        self.window = hou.qt.floatingPanelWindow(self.scene.floatingPanel())
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.panel.set_rop(self.rop)
        self.panel.auto.setChecked(True)
        self.panel.preview_interactive.setChecked(True)
        self.original_paint = type(self.panel.canvas).paintEvent

        def painted(canvas, event):
            self.original_paint(canvas, event)
            if canvas is self.panel.canvas and not canvas.image.isNull():
                self.last_paint = dict(getattr(canvas, "frame_metadata", {}))
                record = dict(
                    at=time.monotonic(),
                    phase=self.report["phase"],
                    mouse_held=self.active_drag,
                    metadata=self.last_paint,
                )
                self.all_paints.append(record)
                if self.case:
                    self.paints.append(record)

        type(self.panel.canvas).paintEvent = painted

        def apply(entry, *args, **kwargs):
            # Avoid the parent benchmark's status() call here: it can pump
            # source capture and would add test work to the measured UI path.
            started = time.monotonic()
            case = self.case
            identity = self.current_input
            actor = (self.camera if case["kind"] == "camera" else self.packed_obj) if case else None
            pose = list(actor.worldTransform().asTuple()) if actor else None
            generation = self.original_apply(entry, *args, **kwargs)
            if entry.key == self.key and generation is not None:
                with entry.session._controller.condition:
                    navigation = entry.session._controller.navigation
                    revision = navigation[2] if navigation is not None and navigation[0] == generation else None
                self.submissions[generation] = dict(
                    start=started,
                    end=time.monotonic(),
                    phase=case["phase"] if case else self.report["phase"],
                    input=identity,
                    pose_preview=bool(kwargs.get("pose_preview")),
                    world=pose,
                    root_revision=revision,
                )
            return generation

        registry.Entry.apply = apply

        def changed(node, event_type, **kwargs):
            if not self.case:
                return
            parm = kwargs.get("parm_tuple")
            if parm is None or parm.name() not in ("t", "r", "s"):
                return
            effect = dict(
                at=time.monotonic(),
                node=node.path(),
                parm=parm.name(),
                value=list(parm.eval()),
                world=list(node.worldTransform().asTuple()),
                input=self.current_input,
                phase=self.report["phase"],
            )
            if node == self.packed_obj and parm.name() == "t":
                # This fixture has identity rotation/scale, no pre-transform or
                # parent. The native callback carries the new parameter value
                # before Houdini invalidates its evaluated world-matrix cache.
                # Construct its expected translated pose from those values.
                expected = list(self.case["object_before"])
                assert expected[:12] == [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
                expected[12:15] = effect["value"]
                effect["parameter_pose"] = expected
            self.effects.append(effect)

        self.node_observer = changed
        for node in (self.camera, self.packed_obj):
            node.addEventCallback((hou.nodeEventType.ParmTupleChanged,), changed)
        self.stage = "arrange"
        self.next_at = time.monotonic() + 0.6

    def configure_scene(self):
        PackedResponse.configure_scene(self)
        copies = self.packed_obj.renderNode()
        assert "box" in copies.parm("viewportlod").menuItems()
        copies.parm("viewportlod").set("box")
        self.rop.setParms(dict(viewer_interactive=1, ipr_resolution_percent=100))
        self.report["expected_device_ids"] = device_selection.set_devices(self.rop, self.device)
        self.report["adaptive_fixture"] = runpy.run_path(
            str(root / "examples/sop-fx-courtyard/adaptive_scene.py"), run_name="viewport_adaptive_fixture"
        )["upgrade"]()

    def ready(self, entry):
        return bool(
            super().ready(entry)
            and not entry.camera_refinement_pending
            and entry.change_serial == entry.applied_serial
            and self.last_paint == self.panel.canvas.frame_metadata
        )

    def save(self):
        if self.report.get("result") == "running":
            self.path.write_text(
                json.dumps(
                    {
                        key: self.report.get(key)
                        for key in ("result", "phase", "device", "host_pid", "viewport_lod", "packed_fixture")
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            super().save()

    @staticmethod
    def rect(rect):
        return [rect.x(), rect.y(), rect.width(), rect.height()]

    def arrange(self):
        self.panel_window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
        screens = QtGui.QGuiApplication.screens()
        screen = max(screens, key=lambda candidate: candidate.availableGeometry().width())
        available = screen.availableGeometry()
        self.report["screens"] = [
            dict(
                name=s.name(),
                geometry=self.rect(s.geometry()),
                available=self.rect(s.availableGeometry()),
                dpr=s.devicePixelRatio(),
                dpi=s.logicalDotsPerInch(),
            )
            for s in screens
        ]
        width = max(600, (available.width() - 60) // 2)
        height = min(1050, available.height() - 80)
        left = QtCore.QRect(available.x() + 10, available.y() + 35, width, height)
        right = QtCore.QRect(left.right() + 20, left.y(), width, height)
        self.window.setGeometry(left)
        self.panel_window.setGeometry(right)
        self.window.show()
        self.panel_window.show()
        self.window.raise_()
        self.panel_window.raise_()
        self.report["placement"] = dict(viewport=self.rect(left), cycles=self.rect(right))
        viewport = self.scene.curViewport()
        viewport.setCamera(self.camera)
        viewport.lockCameraToView(True)
        viewport.exportViewToCameraContinuously(True)
        self.stage = "viewport-ready"
        self.next_at = time.monotonic() + 1.0

    def find_widget(self):
        width, height = self.scene.curViewport().size()[2:]
        matches = [
            widget
            for widget in self.window.findChildren(QtWidgets.QWidget)
            if widget.objectName() == "RE_Window" and widget.width() == width and widget.height() == height
        ]
        assert len(matches) == 1, [
            (w.objectName(), w.width(), w.height())
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window"
        ]
        return matches[0]

    def screenshot(self, name):
        # Window grabs are known to be blank on this host even in stock Houdini.
        # Keep them as capture diagnostics, never proof of physical presentation.
        for label, window in (("viewport", self.window), ("cycles", self.panel_window)):
            path = self.path.with_name(name + "-" + label + ".png")
            assert window.screen().grabWindow(window.winId()).save(str(path))
            self.screenshots.append(str(path))

    def focus_viewport(self):
        self.window.raise_()
        self.window.activateWindow()
        self.scene.setIsCurrentTab()
        self.widget.setFocus(QtCore.Qt.OtherFocusReason)

    def start_case(self, kind):
        self.focus_viewport()
        self.kind = kind
        self.case = dict(
            kind=kind, mode=self.run_mode, repetition=self.test_index % self.bursts, inputs=[], effects=[], paints=[]
        )
        # Reverse alternate gestures to keep the native handle in view during
        # longer runs, without writing a pose while the measured drag is active.
        self.direction = 1 if self.case["repetition"] % 2 == 0 else -1
        self.report["phase"] = self.run_mode + "-" + kind + "-" + str(self.test_index % self.bursts)
        self.case["phase"] = self.report["phase"]
        self.case["camera_before"] = list(self.camera.worldTransform().asTuple())
        self.case["object_before"] = list(self.packed_obj.worldTransform().asTuple())
        self.current_input = None
        self.input_events = {}
        self.effects = []
        self.paints = []
        if kind == "camera":
            self.scene.enterViewState()
            self.start = self.widget.rect().center()
            self.delta = QtCore.QPoint(100, 35) * self.direction
            self.button = QtCore.Qt.MiddleButton
            self.modifiers = QtCore.Qt.AltModifier
        else:
            self.scene.setPwd(hou.node("/obj"))
            self.packed_obj.setSelected(True, clear_all_selected=True)
            self.packed_obj.setCurrent(True)
            self.scene.enterTranslateToolState()
            # Pose has its own selection transaction. Confirm the preselected
            # OBJ before calculating or pressing its native transform handle.
            self.stage = "object-confirm"
            self.next_at = time.monotonic() + 0.3
            return
        self.prepare_hover()

    def prepare_hover(self):
        if self.kind == "object":
            viewport = self.scene.curViewport()
            origin = self.packed_obj.worldTransform().extractTranslates()
            a = viewport.mapToScreen(origin)
            b = viewport.mapToScreen(origin + hou.Vector3(1, 0, 0))
            dx, dy = b[0] - a[0], -(b[1] - a[1])
            length = math.hypot(dx, dy)
            assert length > 1, (a, b)
            self.start = QtCore.QPoint(
                round(a[0] + dx / length * 55), round(self.widget.height() - 1 - a[1] + dy / length * 55)
            )
            self.delta = QtCore.QPoint(round(dx / length * 75), round(dy / length * 75)) * self.direction
            self.button = QtCore.Qt.LeftButton
            self.modifiers = QtCore.Qt.NoModifier
        assert self.widget.rect().contains(self.start) and self.widget.rect().contains(self.start + self.delta), (
            self.start
        )
        QtGui.QCursor.setPos(self.widget.mapToGlobal(self.start))
        QtTest.QTest.mouseMove(self.widget, self.start)
        self.case["drag_pixels"] = dict(start=[self.start.x(), self.start.y()], delta=[self.delta.x(), self.delta.y()])
        self.case["native_state"] = self.scene.currentState()
        self.stage = "hover"
        self.next_at = time.monotonic() + 0.25
        self.save()

    def begin_input(self):
        entry = registry.get(self.key)
        self.case["before_status"] = entry.session.status() if entry else None
        self.case["started"] = time.monotonic()
        QtTest.QTest.mousePress(self.widget, self.button, self.modifiers, self.start)
        self.active_drag = True
        self.case["pressed"] = time.monotonic()
        self.global_origin = self.widget.mapToGlobal(QtCore.QPoint(0, 0))
        self.producer_stop = threading.Event()
        started = time.monotonic()

        # QCoreApplication.postEvent is thread safe. Positions and target were
        # captured on the GUI thread; the producer never reads Houdini/Qt widgets.
        def produce():
            for index in range(self.moves):
                deadline = started + self.interval * (index + 1)
                if self.producer_stop.wait(max(0, deadline - time.monotonic())):
                    return
                fraction = (index + 1) / self.moves
                point = QtCore.QPointF(self.start) + QtCore.QPointF(self.delta) * fraction
                stamp = int(started * 1000) + index + 1
                record = dict(index=index, deadline=deadline, posted=time.monotonic(), stamp=stamp)
                self.case["inputs"].append(record)
                self.input_events[stamp] = record
                event = QtGui.QMouseEvent(
                    QtCore.QEvent.MouseMove,
                    point,
                    QtCore.QPointF(self.global_origin) + point,
                    QtCore.Qt.NoButton,
                    self.button,
                    self.modifiers,
                )
                event.setTimestamp(stamp)
                QtCore.QCoreApplication.postEvent(self.widget, event)

        self.producer = threading.Thread(target=produce, name="Viewport input producer", daemon=True)
        self.producer.start()
        self.stage = "dragging"
        self.next_at = started + self.interval * self.moves + 0.25

    def end_input(self):
        self.producer.join(timeout=1)
        assert not self.producer.is_alive()
        entry = registry.get(self.key)
        if entry:
            self.case["held_status"] = entry.session.status()
            if self.kind == "camera":
                assert (
                    self.case["held_status"]["subdivision_tessellations"]
                    == self.case["before_status"]["subdivision_tessellations"]
                ), self.case
        QtTest.QTest.mouseRelease(self.widget, self.button, self.modifiers, self.start + self.delta)
        self.active_drag = False
        self.case["released"] = time.monotonic()
        self.stage = "settle"
        self.next_at = time.monotonic() + 0.5

    def finish_case(self):
        camera_after = list(self.camera.worldTransform().asTuple())
        object_after = list(self.packed_obj.worldTransform().asTuple())
        self.case.update(
            camera_after=camera_after, object_after=object_after, effects=list(self.effects), paints=list(self.paints)
        )
        target_before = self.case["camera_before" if self.kind == "camera" else "object_before"]
        target_after = camera_after if self.kind == "camera" else object_after
        assert any(abs(a - b) > 1e-5 for a, b in zip(target_before, target_after)), (
            "Native mouse drag had no effect",
            self.case["phase"],
            self.case["native_state"],
        )
        delivered = [event for event in self.case["inputs"] if "delivered" in event]
        assert len(delivered) >= 3, ("Not enough delivered mouse movements", self.case["phase"], len(delivered))
        effects = [
            e for e in self.effects if e["node"] == (self.camera if self.kind == "camera" else self.packed_obj).path()
        ]
        assert len(effects) >= 3, (
            "Native mouse movements did not continuously change the target",
            self.case["phase"],
            len(effects),
        )
        assert len({effect["input"] for effect in effects if effect["input"] is not None}) >= 3, (
            "Only release changed the target",
            self.case["phase"],
        )
        self.case["delivered_events"] = len(delivered)
        self.case["effect_events"] = len(effects)
        self.case["canvas_paints_while_dragging"] = sum(p["mouse_held"] for p in self.paints)
        entry = registry.get(self.key)
        if entry:
            self.case["after_status"] = entry.session.status()
            self.case["controller_events"] = entry.session.diagnostics()["controller"]["events"]
            accepted = {
                (event["submission"], event["physical_generation"]): event
                for event in self.case["controller_events"]
                if event["accepted"]
            }
            previews = set()
            for paint in self.paints:
                metadata = paint["metadata"]
                event = accepted.get((metadata.get("generation"), metadata.get("render_revision")))
                command = self.submissions.get(metadata.get("generation"))
                if event and self.kind == "object":
                    commands = [
                        value
                        for value in self.submissions.values()
                        if value["phase"] == self.case["phase"]
                        and value.get("root_revision") is not None
                        and value["root_revision"] == event["prepared_root_revision"]
                    ]
                    command = commands[0] if commands and event["navigation_kind"] == "objects" else None
                if (
                    paint["mouse_held"]
                    and event
                    and command
                    and command["phase"] == self.case["phase"]
                    and command["world"]
                    and any(abs(a - b) > 1e-5 for a, b in zip(command["world"], target_before))
                ):
                    previews.add(metadata["render_revision"])
            self.case["accepted_pose_images_during_drag"] = len(previews)
            if self.device != "CPU":
                assert previews, ("No updated pose image painted during native drag", self.case["phase"])
            assert self.case["after_status"]["renderer_pid"] == self.worker
            assert tuple(self.case["after_status"]["render_device_ids"]) == tuple(self.report["expected_device_ids"])
            assert self.case["after_status"]["geometry_conversions"] == self.conversions, self.case
        if self.run_mode == "geometry-pending":
            assert self.geometry_entered.is_set() and not self.geometry_release.is_set()
            assert self.panel.entry.session.status()["capture_pending"]
        self.cases.append(self.case)
        self.case = None
        self.test_index += 1
        self.screenshot(self.report["phase"] + "-after")
        if self.test_index < self.bursts * 2:
            self.start_case("camera" if self.test_index < self.bursts else "object")
        elif self.run_mode == "viewport-only":
            assert registry.get(self.key) is None, "Viewport-only baseline unexpectedly started Cycles"
            self.run_mode = "rendering"
            self.test_index = 0
            self.report["phase"] = "starting-ipr"
            QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
            self.stage = "render-ready"
        elif self.run_mode == "rendering":
            self.run_mode = "geometry-pending"
            self.test_index = 0
            self.geometry_release = threading.Event()
            self.geometry_entered = threading.Event()
            release, entered = self.geometry_release, self.geometry_entered

            def held(job):
                assert threading.current_thread() is not threading.main_thread()
                entered.set()
                if not release.wait(40):
                    raise TimeoutError("Viewport workflow did not release the geometry capture")
                return self.prewarm_original(job)

            _native.prewarm_apply = held
            self.deform.parm("rx").set(37)
            self.report["phase"] = "waiting-geometry-capture"
            self.stage = "geometry-barrier"
        else:
            self.geometry_release.set()
            _native.prewarm_apply = self.prewarm_original
            self.report["phase"] = "geometry-refinement"
            self.before = 0
            self.stage = "refine-ready"

    def cleanup(self):
        self.geometry_release.set()
        _native.prewarm_apply = self.prewarm_original
        self.producer_stop.set()
        if self.producer:
            self.producer.join(timeout=2)
        if self.active_drag:
            QtTest.QTest.mouseRelease(self.widget, self.button, self.modifiers, self.start + self.delta)
            self.active_drag = False
        if hasattr(self, "observer"):
            QtWidgets.QApplication.instance().removeEventFilter(self.observer)
        if hasattr(self, "original_paint"):
            type(self.panel.canvas).paintEvent = self.original_paint
        for node in (self.camera, self.packed_obj):
            node.removeEventCallback((hou.nodeEventType.ParmTupleChanged,), self.node_observer)

    def finish(self):
        self.cleanup()
        self.report["drag_cases"] = self.cases
        self.report["submissions"] = self.submissions
        self.report["canvas_paints"] = self.all_paints
        self.report["screenshots"] = self.screenshots
        self.report["final_status"] = self.panel.entry.session.status()
        self.report["final_diagnostics"] = self.panel.entry.session.diagnostics()
        for asset in self.report["assets"]:
            assert hashlib.sha256((root / asset["path"]).read_bytes()).hexdigest() == asset["sha256"]
        HeavyResponse.finish(self)

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            now = time.monotonic()
            assert now - self.began < 150, self.stage
            if now < self.next_at:
                return
            if self.stage == "arrange":
                self.arrange()
            elif self.stage == "viewport-ready":
                self.widget = self.find_widget()
                # Houdini consumes native input in its application filter before
                # QWidget delivery. Observe first at the application boundary.
                self.observer = InputObserver(self)
                QtWidgets.QApplication.instance().installEventFilter(self.observer)
                self.report["viewport_widget"] = dict(
                    size=[self.widget.width(), self.widget.height()],
                    visible=self.widget.isVisible(),
                    window_exposed=self.window.windowHandle().isExposed(),
                )
                assert self.widget.isVisible() and self.window.windowHandle().isExposed()
                self.screenshot("before-input")
                self.start_case("camera")
            elif self.stage == "hover":
                self.begin_input()
            elif self.stage == "object-tool-ready":
                self.prepare_hover()
            elif self.stage == "object-confirm":
                self.focus_viewport()
                QtGui.QCursor.setPos(self.widget.mapToGlobal(self.widget.rect().center()))
                QtTest.QTest.qWait(100)
                QtTest.QTest.keyClick(self.widget, QtCore.Qt.Key_Return)
                self.stage = "object-tool-ready"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "dragging":
                self.end_input()
            elif self.stage == "settle":
                self.finish_case()
            elif self.stage in ("render-ready", "refine-ready"):
                entry = registry.get(self.key)
                if entry and self.ready(entry):
                    if self.stage == "refine-ready":
                        self.finish()
                    else:
                        self.worker = entry.session.status()["renderer_pid"]
                        self.conversions = entry.session.status()["geometry_conversions"]
                        assert entry.session.status()["subdivision_control_faces"] > 0
                        self.start_case("camera")
            elif self.stage == "geometry-barrier":
                if self.geometry_entered.is_set():
                    self.start_case("camera")
        except Exception:
            self.report.update(
                result="failed",
                error=traceback.format_exc(),
                drag_cases=self.cases,
                failing_case=self.case,
                screenshots=self.screenshots,
            )
            self.cleanup()
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = ViewportDrag()
