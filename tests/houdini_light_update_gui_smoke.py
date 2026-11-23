# SPDX-License-Identifier: Apache-2.0
"""One render viewer: automatic light edits and viewport object picking."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets, QtTest
from cycles_houdini import light_properties, light_viewer_state, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[])
        self.states = []
        original_enter = light_viewer_state.LightState.onEnter

        def record_enter(state, kwargs):
            try:
                original_enter(state, kwargs)
                self.states.append(state)
            except Exception:
                self.report["handle_error"] = traceback.format_exc()
                raise

        light_viewer_state.LightState.onEnter = record_enter
        original_handle = light_viewer_state.LightState.onHandleToState

        def record_handle(state, kwargs):
            self.report["events"].append(dict(handle=kwargs["handle"], modified=list(kwargs["mod_parms"])))
            original_handle(state, kwargs)

        light_viewer_state.LightState.onHandleToState = record_handle
        self.handle_values = {}
        original_to_handle = light_viewer_state.LightState.onStateToHandle

        def record_to_handle(state, kwargs):
            original_to_handle(state, kwargs)
            self.handle_values[kwargs["handle"]] = dict(kwargs["parms"])

        light_viewer_state.LightState.onStateToHandle = record_to_handle
        self.started = time.monotonic()
        self.stage = 0
        self.generation = 0
        self.busy = False
        obj = hou.node("/obj").createNode("geo", "receiver", run_init_scripts=False)
        grid = obj.createNode("grid")
        grid.setParms(dict(orient=0, rows=2, cols=2, sizex=8, sizey=8))
        camera = hou.node("/obj").createNode("cam", "camera")
        camera.setParms(dict(tz=8, projection=1, orthowidth=8))
        self.light = hou.node("/obj").createNode(light_properties.LIGHT_TYPE, "cycles_area")
        self.light.setParms(dict(tx=-2, tz=4, light_intensity=500, singlesided=1))
        self.native = hou.node("/obj").createNode("hlight::2.0", "native_area")
        self.native.setParms(dict(tx=3, ty=2, tz=4, light_type="grid", light_enable=0))
        self.rop = hou.node("/out").createNode("cycles_ipr", "light_update")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=obj.path(),
                lights=self.light.path(),
                camera=camera.path(),
                resx=192,
                resy=144,
                samples=32,
                ipr_samples=32,
                use_material_assignments=0,
                material_kind="diffuse",
                allowmotionblur=0,
                use_denoise=0,
                ipr_use_denoise=0,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.viewer.setPwd(hou.node("/obj"))
        self.window = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
        self.window.resize(900, 700)
        self.viewer.curViewport().changeType(hou.geometryViewportType.Front)
        self.viewer.curViewport().frameBoundingBox(hou.BoundingBox(-5, -4, -3, 5, 4, 5))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.at = time.monotonic() + 1
        hou.ui.addEventLoopCallback(self.tick)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def ready(self):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        assert not entry.error, entry.error
        assert status["state"] != "failed", status
        info = entry.session.frame_info()
        if not info or not info["complete"] or info["generation"] <= self.generation:
            return False
        if self.panel._live_generation != info["generation"]:
            return False
        self.generation = info["generation"]
        self.report["events"].append(dict(stage=self.stage, generation=self.generation, status=status))
        return True

    def image(self):
        return np.frombuffer(self.panel.entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()

    def viewport_widget(self):
        return next(
            w
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window" and w.width() == self.viewer.curViewport().size()[2]
        )

    def confirm_selection(self):
        self.viewer.setIsCurrentTab()
        self.window.raise_()
        self.window.activateWindow()
        widget = self.viewport_widget()
        widget.setFocus(QtCore.Qt.OtherFocusReason)
        # Houdini routes viewport hotkeys using the pane under the real cursor,
        # not just the QWidget receiving a synthetic key event.
        QtGui.QCursor.setPos(widget.mapToGlobal(widget.rect().center()))
        QtTest.QTest.qWait(100)
        QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Return)

    def pick(self, node, modifiers=QtCore.Qt.NoModifier):
        viewport = self.viewer.curViewport()
        widget = self.viewport_widget()
        p = viewport.mapToScreen(node.worldTransform().extractTranslates())
        point = QtCore.QPoint(round(p[0]), widget.height() - 1 - round(p[1]))
        self.report["events"].append(
            dict(pick=node.path(), pixel=[point.x(), point.y()], state_before=self.viewer.currentState())
        )
        self.window.raise_()
        self.window.activateWindow()
        widget.setFocus(QtCore.Qt.OtherFocusReason)
        QtTest.QTest.mouseClick(widget, QtCore.Qt.LeftButton, modifiers, point)

    def drag(self, start, end):
        widget = self.viewport_widget()
        viewport = self.viewer.curViewport()

        def pixel(world):
            p = viewport.mapToScreen(hou.Vector3(world))
            return QtCore.QPointF(p[0], widget.height() - 1 - p[1])

        start, end = pixel(start), pixel(end)
        # Native handles determine the active control on hover. Pressing at a
        # new coordinate without a preceding move can drag the previous control.
        QtGui.QCursor.setPos(widget.mapToGlobal(start.toPoint()))
        QtTest.QTest.mouseMove(widget, start.toPoint())
        QtTest.QTest.qWait(100)
        QtTest.QTest.mousePress(widget, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start.toPoint())
        for step in range(1, 9):
            point = start + (end - start) * (step / 8)
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

    def tick(self):
        if self.busy or time.monotonic() < self.at:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 200, (self.stage, self.report)
            if self.stage == 0:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.render_button.click()
            elif self.stage == 1:
                if not self.ready():
                    return
                self.before = self.image()
                self.light.parm("tx").set(2)
            elif self.stage == 2:
                if not self.ready():
                    return
                delta = float(np.mean(np.abs(self.image() - self.before)))
                assert delta > 0.005, delta
                self.report["events"].append(dict(name="Automatic transform pixels", delta=delta))
                self.before = self.image()
                self.light.parm("light_intensity").set(1500)
            elif self.stage == 3:
                if not self.ready():
                    return
                delta = float(np.mean(np.abs(self.image() - self.before)))
                assert delta > 0.005, delta
                self.report["events"].append(dict(name="Automatic intensity pixels", delta=delta))
                hou.clearAllSelected()
                self.viewer.setCurrentState("pose")
            elif self.stage == 4:
                self.pick(self.native)
            elif self.stage == 5:
                self.report["events"].append(
                    dict(
                        name="Native viewport selection",
                        selected=[n.path() for n in hou.selectedNodes()],
                        state=self.viewer.currentState(),
                    )
                )
                self.window.screen().grabWindow(self.window.winId()).save(str(self.path.with_suffix(".native.png")))
                self.pick(self.light)
            elif self.stage == 6:
                if self.viewer.currentState() != light_viewer_state.STATE_NAME:
                    assert self.light in hou.selectedNodes(), hou.selectedNodes()
                    self.confirm_selection()
                    self.at = time.monotonic() + 0.7
                    return
                self.report["events"].append(
                    dict(
                        name="Cycles viewport selection",
                        selected=[n.path() for n in hou.selectedNodes()],
                        state=self.viewer.currentState(),
                    )
                )
                self.window.screen().grabWindow(self.window.winId()).save(str(self.path.with_suffix(".cycles.png")))
                assert any(state.active and state.node == self.light for state in self.states)
                self.before = self.image()
                self.old_tx = self.light.evalParm("tx")
                self.conversions = self.panel.entry.session.status()["geometry_conversions"]
                self.drag((4.0, 0, 4), (4.8, 0, 4))
            elif self.stage == 7:
                assert abs(self.light.evalParm("tx") - self.old_tx) > 0.2, self.light.parmTuple("t").eval()
                assert not any(e.get("handle") == light_viewer_state.SPOT for e in self.report["events"]), (
                    "Hidden spot handle intercepted an area-light drag"
                )
                assert self.light.node("cycles_light_guide").type().category() == hou.sopNodeTypeCategory()
                if not self.ready():
                    return
                delta = float(np.mean(np.abs(self.image() - self.before)))
                assert delta > 0.005, delta
                assert self.panel.entry.session.status()["geometry_conversions"] == self.conversions
                self.report["events"].append(dict(name="Viewport handle drag updates the rendered light", delta=delta))
                # Selection modifiers belong to Select, not a Ctrl-click on
                # the transform handle still covering the emitter in Pose.
                self.viewer.setCurrentState("select")
                self.pick(self.native)
            elif self.stage == 8:
                assert self.native in hou.selectedNodes(), hou.selectedNodes()
                self.pick(self.light, QtCore.Qt.ShiftModifier)
            elif self.stage == 9:
                assert {self.light, self.native} <= set(hou.selectedNodes()), hou.selectedNodes()
                self.pick(self.light, QtCore.Qt.ControlModifier)
            elif self.stage == 10:
                assert self.native in hou.selectedNodes() and self.light not in hou.selectedNodes(), hou.selectedNodes()
                self.pick(self.light)
            elif self.stage == 11:
                assert hou.selectedNodes() == (self.light,), hou.selectedNodes()
                self.enter_attempts = 1
                self.confirm_selection()
            elif self.stage == 12:
                if self.viewer.currentState() != light_viewer_state.STATE_NAME and self.enter_attempts < 3:
                    self.enter_attempts += 1
                    self.confirm_selection()
                    self.at = time.monotonic() + 0.7
                    return
                assert self.viewer.currentState() == light_viewer_state.STATE_NAME, self.viewer.currentState()
                self.report["events"].append(
                    dict(name="Viewport selection supports additive/removal modifiers and Enter enters Cycles handles")
                )
                self.panel.dispose()
                registry.close_all()
                self.viewer.enterViewState()
                example = artifacts_root() / "examples/sop-fx-courtyard"
                hou.hipFile.load(str(example / "sop-fx-courtyard-adaptive.hiplc"), suppress_save_prompt=True)
                hou.putenv("OCIO", str(example / "showcase.ocio"))
                hou.Color.reloadOCIO()
                self.rop = hou.node("/out/cycles_sop_courtyard")
                from cycles_houdini import device_selection

                device_selection.set_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
                set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
                self.rop.setParms(dict(resx=192, resy=144, samples=8, ipr_samples=8, ipr_use_denoise=0, use_denoise=0))
                self.light = next(n for n in hou.node("/obj").children() if light_properties.is_cycles_light(n))
                assert self.light.type().definition().extraInfoValue("subtype") == "light"
                self.tab.close()
                self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = None
                self.generation = 0
            elif self.stage == 13:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.render_button.click()
            elif self.stage == 14:
                if not self.ready():
                    return
                self.before = self.image()
                self.conversions = self.panel.entry.session.status()["geometry_conversions"]
                self.light.parm("tx").set(self.light.evalParm("tx") + 4)
                self.light.parm("ry").set(self.light.evalParm("ry") + 25)
            elif self.stage == 15:
                if not self.ready():
                    return
                delta = float(np.mean(np.abs(self.image() - self.before)))
                assert delta > 0.0001, delta
                assert self.panel.entry.session.status()["geometry_conversions"] == self.conversions
                self.report["events"].append(
                    dict(
                        name="Saved adaptive showcase light moves automatically without geometry conversion",
                        delta=delta,
                    )
                )
                self.viewer.setPwd(hou.node("/obj"))
                self.viewer.setCurrentState("select")
                hou.clearAllSelected()
                self.light.parmTuple("r").set((0, 0, 0))
                center = self.light.worldTransform().extractTranslates()
                self.viewer.curViewport().changeType(hou.geometryViewportType.Front)
                self.viewer.curViewport().frameBoundingBox(
                    hou.BoundingBox(
                        center[0] - 5, center[1] - 4, center[2] - 4, center[0] + 5, center[1] + 4, center[2] + 4
                    )
                )
                self.pick(self.light)
            elif self.stage == 16:
                assert hou.selectedNodes() == (self.light,), hou.selectedNodes()
                self.report["events"].append(dict(name="Saved showcase viewport pick selects the named light"))
                self.viewer.setCurrentState(light_viewer_state.STATE_NAME, generate=hou.stateGenerateMode.Enter)
            elif self.stage == 17:
                assert not self.report.get("handle_error"), self.report.get("handle_error")
                active = [state for state in self.states if state.active and state.node == self.light]
                assert len(active) == 1 and active[0].signature is not None
                assert self.viewer.currentState() == light_viewer_state.STATE_NAME
                if not self.ready():
                    return
                self.before = self.image()
                self.old_tx = self.light.evalParm("tx")
                self.conversions = self.panel.entry.session.status()["geometry_conversions"]
                self.window.screen().grabWindow(self.window.winId()).save(
                    str(self.path.with_suffix(".showcase-handles.png"))
                )
                # Native Pose handle uses the same authored owner after picking.
                self.viewer.setCurrentState("pose")
            elif self.stage == 18:
                # Switching from the custom state starts Pose's selection step.
                # Pick the light as an artist does before dragging its handles.
                self.pick(self.light)
            elif self.stage == 19:
                assert self.light in hou.selectedNodes(), hou.selectedNodes()
                # Pose explicitly prompts to confirm its object selection with
                # Enter in this saved desktop; selected glyphs are not handles.
                self.confirm_selection()
            elif self.stage == 20:
                self.window.raise_()
                self.window.activateWindow()
                transform = hou.Handle(self.viewer, light_viewer_state.TRANSFORM)
                self.report["transform_activation_before"] = dict(
                    active=transform.isActive(), enabled=transform.isEnabled(), visible=transform.visible()
                )
                self.window.screen().grabWindow(self.window.winId()).save(
                    str(self.path.with_suffix(".showcase-pose.png"))
                )
                center = self.light.worldTransform().extractTranslates()
                self.report["drag_before"] = dict(
                    state=self.viewer.currentState(),
                    world=list(self.light.worldTransform().asTuple()),
                    parms={name: self.light.parmTuple(name).eval() for name in ("t", "r", "s", "p", "areasize")},
                )
                self.report["handle_values"] = self.handle_values
                self.report["active_handles"] = [
                    dict(
                        node=state.node.path(),
                        handles={
                            name: dict(visible=handle.visible(), active=handle.isActive())
                            for name, handle in state.handles.items()
                        },
                    )
                    for state in self.states
                    if state.active and state.node
                ]
                # Hit the native OBJ translation arrow outside the size controls.
                self.drag((center[0] + 2.0, center[1], center[2]), (center[0] + 2.8, center[1], center[2]))
            elif self.stage == 21:
                self.report["drag_after"] = dict(
                    state=self.viewer.currentState(),
                    world=list(self.light.worldTransform().asTuple()),
                    parms={name: self.light.parmTuple(name).eval() for name in ("t", "r", "s", "p", "areasize")},
                )
                assert abs(self.light.evalParm("tx") - self.old_tx) > 0.2, self.light.parmTuple("t").eval()
                if not self.ready():
                    return
                delta = float(np.mean(np.abs(self.image() - self.before)))
                assert delta > 0.0001, delta
                assert self.panel.entry.session.status()["geometry_conversions"] == self.conversions
                self.report["events"].append(
                    dict(name="Saved showcase viewport drag updates actual rendered light", delta=delta)
                )
                self.finish()
                return
            self.stage += 1
            self.at = time.monotonic() + 0.7
            self.save()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        self.report.update(result="failed" if error else "passed", error=error, stage=self.stage)
        self.save()
        hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


workflow = Workflow()
