# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: actual native light/shape handles, drags, undo and live SOP IPR."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtGui, QtWidgets, QtTest
from cycles_houdini import light_properties, light_viewer_state, registry


class HandleWorkflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.report = dict(result="running", events=[], callbacks=[], host_pid=os.getpid())
        self.began = time.monotonic()
        self.busy = False
        self.instances = []
        self.phase = "native-handles"
        self.stage = 0
        original_init = light_viewer_state.LightState.__init__
        original_to_handle = light_viewer_state.LightState.onStateToHandle
        original_to_state = light_viewer_state.LightState.onHandleToState

        def capture_init(state, *args, **kwargs):
            self.instances.append(state)
            try:
                original_init(state, *args, **kwargs)
            except Exception:
                self.report["factory_error"] = traceback.format_exc()
                self.save()
                raise

        def capture_to_handle(state, kwargs):
            original_to_handle(state, kwargs)
            self.last_values[kwargs["handle"]] = dict(kwargs["parms"])

        def capture_to_state(state, kwargs):
            self.report["callbacks"].append(
                dict(
                    handle=kwargs["handle"],
                    modified=list(kwargs["mod_parms"]),
                    phase=self.phase,
                    parms=dict(kwargs["parms"]),
                )
            )
            original_to_state(state, kwargs)

        self.last_values = {}
        light_viewer_state.LightState.__init__ = capture_init
        light_viewer_state.LightState.onStateToHandle = capture_to_handle
        light_viewer_state.LightState.onHandleToState = capture_to_state
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.SceneViewer)
        self.viewer.setPwd(hou.node("/obj"))
        self.window = hou.qt.floatingPanelWindow(self.viewer.floatingPanel())
        self.window.resize(1100, 850)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.light = hou.node("/obj").createNode(light_properties.LIGHT_TYPE, "HANDLE_AREA")
        self.light.setParms(dict(cycles_light_area_shape="rectangle", areasizex=4, areasizey=2))
        self.light.setSelected(True, clear_all_selected=True)
        self.light.setCurrent(True)
        self.viewer.curViewport().changeType(hou.geometryViewportType.Front)
        self.viewer.curViewport().frameBoundingBox(hou.BoundingBox(-5, -4, -3, 5, 4, 3))
        assert self.light.defaultState() == light_viewer_state.STATE_NAME, self.light.defaultState()
        self.report["automatic_state_registered"] = hou.ui.isRegisteredViewerState(light_viewer_state.STATE_NAME)
        self.save()
        assert self.report["automatic_state_registered"], "The asset did not register its native handle state"
        self.report["state_info"] = hou.ui.viewerStateInfo([light_viewer_state.STATE_NAME])
        self.save()
        self.viewer.setCurrentState(light_viewer_state.STATE_NAME, generate=hou.stateGenerateMode.Enter)
        self.at = time.monotonic() + 1
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def state(self):
        assert self.viewer.currentState() == light_viewer_state.STATE_NAME, self.viewer.currentState()
        states = [state for state in self.instances if state.active and state.node == self.light]
        assert len(states) == 1, [(state.active, str(state.node)) for state in self.instances]
        return states[0]

    def capture(self, name):
        self.window.screen().grabWindow(self.window.winId()).save(str(self.path.with_suffix("." + name + ".png")))
        state = self.state()
        self.report["events"].append(
            dict(
                name=name,
                state=self.viewer.currentState(),
                kind=self.light.evalParm("cycles_light_type"),
                shape=self.light.evalParm("cycles_light_area_shape"),
                areasize=light_properties.area_size(self.light),
                scale=self.light.parmTuple("s").eval(),
                radius=self.light.evalParm("cycles_light_radius"),
                handle_values=dict(self.last_values),
                activation={name: handle.isActive() for name, handle in state.handles.items()},
                visibility={name: handle.visible() for name, handle in state.handles.items()},
            )
        )
        self.save()

    def drag(self, start_world, end_world):
        # Deliver actual mouse events to the native viewport's Qt widget. The
        # renderer/state callbacks are observed, never called by the test.
        viewport = self.viewer.curViewport()
        widget = next(
            w
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window" and w.width() == viewport.size()[2]
        )

        def pixel(world):
            point = viewport.mapToScreen(hou.Vector3(world))
            return QtCore.QPointF(point[0], widget.height() - 1 - point[1])

        start, end = pixel(start_world), pixel(end_world)
        self.drag_pixels(widget, start, end)
        self.report["last_drag"].update(start_world=start_world, end_world=end_world)

    def drag_pixels(self, widget, start, end):
        self.window.raise_()
        self.window.activateWindow()
        widget.setFocus()
        QtGui.QCursor.setPos(widget.mapToGlobal(start.toPoint()))
        QtTest.QTest.qWait(100)
        for event_type, position, button, buttons in (
            (QtCore.QEvent.MouseMove, start, QtCore.Qt.NoButton, QtCore.Qt.NoButton),
            (QtCore.QEvent.MouseButtonPress, start, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton),
        ):
            event = QtGui.QMouseEvent(
                event_type,
                position,
                QtCore.QPointF(widget.mapToGlobal(position.toPoint())),
                button,
                buttons,
                QtCore.Qt.NoModifier,
            )
            QtWidgets.QApplication.sendEvent(widget, event)
            QtTest.QTest.qWait(40)
        for step in range(1, 9):
            position = start + (end - start) * (step / 8)
            event = QtGui.QMouseEvent(
                QtCore.QEvent.MouseMove,
                position,
                QtCore.QPointF(widget.mapToGlobal(position.toPoint())),
                QtCore.Qt.NoButton,
                QtCore.Qt.LeftButton,
                QtCore.Qt.NoModifier,
            )
            QtWidgets.QApplication.sendEvent(widget, event)
            QtTest.QTest.qWait(35)
        event = QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonRelease,
            end,
            QtCore.QPointF(widget.mapToGlobal(end.toPoint())),
            QtCore.Qt.LeftButton,
            QtCore.Qt.NoButton,
            QtCore.Qt.NoModifier,
        )
        QtWidgets.QApplication.sendEvent(widget, event)
        self.report["last_drag"] = dict(start_pixel=[start.x(), start.y()], end_pixel=[end.x(), end.y()])

    def viewport_widget(self):
        return next(
            w
            for w in self.window.findChildren(QtWidgets.QWidget)
            if w.objectName() == "RE_Window" and w.width() == self.viewer.curViewport().size()[2]
        )

    def ready(self):
        entry = self.panel.entry
        if entry and entry.error:
            raise AssertionError(entry.error)
        return (
            entry
            and self.panel.canvas.generation > self.before
            and self.panel.canvas.generation == entry.session.status()["generation"]
        )

    def tick(self):
        if self.busy or time.monotonic() < self.at:
            return
        self.busy = True
        try:
            if time.monotonic() - self.began > 180:
                raise TimeoutError(str(self.stage))
            if self.stage == 0:
                state = self.state()
                assert hou.Handle(self.viewer, light_viewer_state.TRANSFORM).isValid()
                assert state.handles[light_viewer_state.RECTANGLE].visible()
                self.capture("rectangle-before")
                self.old_size = light_properties.area_size(self.light)
                self.old_t = self.light.parmTuple("t").eval()
                self.drag((2, 1, 0), (2.6, 1.4, 0))
            elif self.stage == 1:
                self.capture("rectangle-after")
                assert self.report["callbacks"], "Viewport drag did not enter the native handle callback"
                assert light_properties.area_size(self.light) != self.old_size, self.report["last_drag"]
                assert self.light.parmTuple("t").eval() == self.old_t
                assert not self.state().undo_open
                hou.undos.performUndo()
            elif self.stage == 2:
                assert light_properties.area_size(self.light) == self.old_size
                self.light.parm("cycles_light_area_shape").set("ellipse")
            elif self.stage == 3:
                self.capture("ellipse-before")
                assert self.state().handles[light_viewer_state.ELLIPSE].visible()
                assert not self.state().handles[light_viewer_state.RECTANGLE].visible()
                self.previous_calls = len(self.report["callbacks"])
                # The edge arrow constrains the drag to X. A corner drag has
                # two degrees of freedom and integer pixels can change Y too.
                self.drag((2.1, 0, 0), (2.8, 0, 0))
            elif self.stage == 4:
                self.capture("ellipse-after")
                assert len(self.report["callbacks"]) > self.previous_calls
                assert self.light.evalParm("areasizex") > self.old_size[0]
                assert self.light.evalParm("areasizey") == self.old_size[1]
                self.light.setParms(dict(cycles_light_type="point", cycles_light_radius=0.65))
            elif self.stage == 5:
                self.capture("radius-before")
                assert self.state().handles[light_viewer_state.RADIUS].visible()
                self.previous_calls = len(self.report["callbacks"])
                self.drag((0.65, 0.65, 0.65), (1.0, 0.65, 0.65))
            elif self.stage == 6:
                self.capture("radius-after")
                assert len(self.report["callbacks"]) > self.previous_calls
                assert self.light.evalParm("cycles_light_radius") > 0.65
                self.parent = hou.node("/obj").createNode("null", "HANDLE_PARENT")
                self.parent.parmTuple("s").set((3, 2, 1))
                self.light.setInput(0, self.parent)
            elif self.stage == 7:
                self.capture("radius-with-scaled-parent")
                assert (
                    abs(
                        self.last_values[light_viewer_state.RADIUS]["sizex"] * 3
                        - 2 * self.light.evalParm("cycles_light_radius")
                    )
                    < 1e-6
                )
                self.light.setParms(
                    dict(cycles_light_type="spot", cycles_light_spot_angle=70, cycles_light_spot_smooth=0.4, ry=90)
                )
                self.parent.parmTuple("s").set((1, 1, 1))
            elif self.stage == 8:
                assert self.state().handles[light_viewer_state.SPOT].visible()
                widget = self.viewport_widget()
                widget.setFocus()
                center = self.viewer.curViewport().mapToScreen(hou.Vector3(0, 0, 0))
                QtTest.QTest.mouseMove(widget, QtCore.QPoint(int(center[0]), int(widget.height() - 1 - center[1])))
                QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Z)
            elif self.stage == 9:
                self.capture("spot-cone-before")
                self.previous_calls = len(self.report["callbacks"])
                self.old_angles = (
                    self.light.evalParm("cycles_light_spot_angle"),
                    self.light.evalParm("cycles_light_spot_smooth"),
                )
                self.old_t = self.light.parmTuple("t").eval()
                self.old_r = self.light.parmTuple("r").eval()
                # SideFX's native cone angle is edited by its red ring, not the
                # square at the end of the blue orientation arc. Viewed edge-on,
                # the ring projects to a line at the guide's three-unit distance.
                radius = 3 * math.tan(math.radians(self.old_angles[0] * 0.5))
                self.drag((-3, radius * 0.75, 0), (-3, radius * 0.75 + 0.4, 0))
            elif self.stage == 10:
                self.capture("spot-cone-after")
                calls = self.report["callbacks"][self.previous_calls :]
                assert any(
                    c["handle"] == light_viewer_state.SPOT and set(c["modified"]) & {"coneangle", "conedelta"}
                    for c in calls
                ), [(c["handle"], c["modified"]) for c in calls]
                angles = (
                    self.light.evalParm("cycles_light_spot_angle"),
                    self.light.evalParm("cycles_light_spot_smooth"),
                )
                assert angles != self.old_angles, angles
                assert self.light.parmTuple("t").eval() == self.old_t and self.light.parmTuple("r").eval() == self.old_r
                assert not self.state().undo_open
                hou.undos.performUndo()
                assert (
                    self.light.evalParm("cycles_light_spot_angle"),
                    self.light.evalParm("cycles_light_spot_smooth"),
                ) == self.old_angles
                QtTest.QTest.keyClick(self.viewport_widget(), QtCore.Qt.Key_O)
                self.light.setParms(
                    dict(cycles_light_type="grid", cycles_light_area_shape="square", singlesided=0, ry=0)
                )
                self.light.setParms(dict(areasizex=2, areasizey=7))
                self.light.setPreTransform(hou.hmath.buildTranslate((1, 2, -1)))
                self.parent.parmTuple("t").set((-2, 1, 1))
                self.parent.parmTuple("s").set((3, 2, 1))
            elif self.stage == 11:
                self.capture("scaled-parented-square")
                size = self.last_values[light_viewer_state.RECTANGLE]
                assert abs(size["sizex"] * 3 - 2) < 1e-6 and abs(size["sizey"] * 2 - 2) < 1e-6, size
                assert self.light.evalParm("areasizey") == 7
                self.parent.parmTuple("s").set((3, 2, -1))
                self.parent.parm("tx").set(-1)
            elif self.stage == 12:
                self.capture("parent-moved")
                center = self.light.worldTransform().extractTranslates()
                values = self.last_values[light_viewer_state.RECTANGLE]
                handle_center = (
                    hou.Vector3(tuple(values["center" + axis] for axis in "xyz"))
                    * self.light.preTransform()
                    * self.light.parentAndSubnetTransform()
                )
                assert (handle_center - center).length() < 1e-6
                self.light.parm("cycles_light_type").set("sun")
            elif self.stage == 13:
                self.capture("sun-direction")
                assert all(not self.state().handles[name].visible() for name in light_viewer_state.SHAPES)
                # Switch scene/state before deletion, then test a fresh default
                # state in the heavy SOP fixture and its actual external worker.
                self.viewer.enterViewState()
                self.light.destroy()
                self.parent.destroy()
                from cycles_houdini.panel import RenderPanel

                example = artifacts_root() / "examples/sop-fx-courtyard"
                hou.hipFile.load(str(example / "sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True)
                hou.putenv("OCIO", str(example / "showcase.ocio"))
                hou.Color.reloadOCIO()
                obj = hou.node("/obj/fx_courtyard")
                obj.node("NOISE_DISPLACED_GROUND").parm("resolution").set(560)
                obj.node("NOISE_DISPLACED_ROCKS").parm("count").set(80)
                obj.node("ROCK_PROTOTYPE_SPHERE").setParms(dict(rows=36, cols=72))
                obj.node("CURVE_VEGETATION_AND_CABLES").parm("count").set(600)
                obj.node("SPARK_PARTICLES").parm("count").set(1200)
                obj.node("INSTANCE_GRID").setParms(dict(rows=17, cols=21))
                obj.node("STABLE_INSTANCE_POINTS").parm("snippet").set(
                    "int x=int(rint((@P.x/8.64+.5)*20)); int z=int(rint((@P.z/6.24+.5)*16));\n"
                    "if(abs(x-10)<2 && abs(z-8)<2) removepoint(0,@ptnum);\n"
                    "i@id=1000+z*21+x; v@N={0,0,1}; v@up={0,1,0};"
                )
                self.rop = hou.node("/out/cycles_sop_courtyard")
                from cycles_houdini import device_selection

                device_selection.set_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
                set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
                self.rop.setParms(
                    dict(source_mode="objects", vobject="fx_courtyard", lights="*", samples=65536, resx=960, resy=540)
                )
                import cycles_houdini as ch
                from cycles_houdini import native_scene

                capture = ch._native.create(
                    os.environ["CYCLES_HOUDINI_GUI_DEVICE"], 960, 540, 65536, True, False, True, False, None, True
                )
                owned = ch._native.apply(capture, native_scene.capture(self.rop), hou.time())
                self.report["fixture"] = ch._native.snapshot_info(owned)
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                ch._native.close(capture)
                del owned, capture
                self.viewer.setPwd(hou.node("/obj"))
                self.viewer.curViewport().setCamera(hou.node("/obj/courtyard_camera"))
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                self.panel.set_rop(self.rop)
                self.panel.window().resize(1100, 850)
                self.before = 0
                self.phase = "heavy-sop-ipr"
                self.panel.render_button.click()
            elif self.stage == 14:
                if not self.ready():
                    return
                status = self.panel.entry.session.status()
                self.conversions = status["geometry_conversions"]
                self.before = status["generation"]
                self.report["initial_status"] = status
                self.report["render_geometry_status"] = {
                    name: value for name, value in status.items() if "triangle" in name or "object" in name
                }
                self.light = hou.node("/obj").createNode(light_properties.LIGHT_TYPE, "HANDLE_LIVE_AREA")
                self.light.setParms(dict(tx=-3, ty=5, tz=4, rx=-50, ry=-30, light_intensity=2500))
                self.light.setParms(dict(areasizex=3, areasizey=2))
                self.light.setSelected(True, clear_all_selected=True)
                self.light.setCurrent(True)
                self.viewer.enterCurrentNodeState()
            elif self.stage == 15:
                if not self.ready():
                    return
                self.capture("live-area-added")
                self.before = self.panel.entry.session.status()["generation"]
                self.worker_pid = self.panel.entry.session.status()["renderer_pid"]
                self.light.setParms(dict(areasizex=4.5, cycles_light_area_shape="ellipse", tx=-2.6))
            elif self.stage == 16:
                if not self.ready():
                    return
                self.capture("live-area-edited")
                status = self.panel.entry.session.status()
                assert status["geometry_conversions"] == self.conversions
                assert status["renderer_pid"] == self.worker_pid
                self.report["edited_status"] = status
                self.before = status["generation"]
                self.light.destroy()
            elif self.stage == 17:
                if not self.ready():
                    return
                assert self.panel.entry.session.status()["geometry_conversions"] == self.conversions
                self.report["events"].append(
                    dict(name="Deleted the selected light during heavy SOP IPR; unchanged geometry retained")
                )
                self.panel.dispose()
                registry.close_all()
                self.viewer.enterViewState()
                self.report["result"] = "passed"
                self.save()
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(0, suppress_save_prompt=True))
                return
            self.stage += 1
            self.at = time.monotonic() + 0.6
            self.save()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc(), stage=self.stage)
            self.save()
            hou.ui.removeEventLoopCallback(self.callback)
            hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
        finally:
            self.busy = False


workflow = HandleWorkflow()
