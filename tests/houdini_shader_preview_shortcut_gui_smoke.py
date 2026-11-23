# SPDX-License-Identifier: Apache-2.0
"""Native Ctrl+Shift+click delivery, output cycling and Undo with one viewer."""

import json, os, time, traceback
from pathlib import Path
import hou, nodegraphhooks
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry
from shader_bypass_preview_fixture import Fixture


class Workflow:
    def __init__(self):
        assert getattr(nodegraphhooks.createEventHandler, "cycles_preview_hook", False), (
            "UI-ready preview hook was not installed"
        )
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.fixture = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f = self.fixture
        self.split = f.builder.createNode("cycles::separate_xyz::1", "split_preview")
        self.split.parmTuple("vector").set((0.1, 0.2, 0.3))
        self.outer = ch.create_subnet(f.builder, "outer_preview")
        self.inner = ch.create_subnet(self.outer, "inner_preview")
        self.scalar = self.inner.createNode("cycles::value::1", "value_preview")
        self.scalar.parm("value").set(0.42)
        self.color = self.inner.createNode("cycles::color::1", "color_preview")
        self.color.parmTuple("value").set((0.25, 0.5, 0.75))
        for index, node in enumerate((f.color, f.value, f.math, f.range, self.split, self.outer)):
            node.setPosition(hou.Vector2(index * 3, 0))
        self.scalar.setPosition(hou.Vector2(0, 0))
        self.color.setPosition(hou.Vector2(4, 0))
        self.network = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(800, 600))
        self.network.setPwd(f.builder)
        self.window = hou.qt.floatingPanelWindow(self.network.floatingPanel())
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        available = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.window.move(available.topLeft() + QtCore.QPoint(10, 30))
        self.window.show()
        self.panel = None
        self.stage = -1
        self.phase = "mount"
        self.generation = 0
        self.conversions = None
        self.started = time.monotonic()
        self.next_at = 0
        self.busy = False
        self.target = None
        self.events = []
        self.original_hook = nodegraphhooks.createEventHandler

        def observed(event, actions):
            if event.eventtype == "mousedown":
                self.events.append(
                    dict(
                        node=event.selected.item.path() if isinstance(event.selected.item, hou.Node) else None,
                        component=event.selected.name,
                        ctrl=event.modifierstate.ctrl,
                        shift=event.modifierstate.shift,
                        alt=event.modifierstate.alt,
                        left=event.mousestate.lmb,
                    )
                )
            return self.original_hook(event, actions)

        nodegraphhooks.createEventHandler = observed
        self.report = dict(
            result="running",
            events=[],
            scope="Synthetic Qt mouse input observed by Houdini native network events; root/nested previews and Undo, one Cycles Viewer with accepted image delivery. Physical-screen capture is separate.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def prepare(self):
        f = self.fixture
        targets = {1: f.value, 2: f.color, 3: self.split, 4: self.split, 7: self.scalar, 8: self.color}
        if self.stage in (5, 9, 10):
            hou.undos.performUndo()
            self.phase = "wait"
            return
        if self.stage == 6:
            hou.undos.performRedo()
            self.phase = "wait"
            return
        self.target = targets[self.stage]
        if self.network.pwd() != self.target.parent():
            self.network.setPwd(self.target.parent())
        self.phase = "frame"
        self.next_at = time.monotonic() + 0.2

    def frame_target(self):
        rect = self.network.itemRect(self.target)
        center = rect.center()
        self.network.setVisibleBounds(
            hou.BoundingRect(center - hou.Vector2(4, 3), center + hou.Vector2(4, 3)),
            set_center_when_scale_rejected=True,
        )
        self.phase = "click"
        self.next_at = time.monotonic() + 0.4

    def click(self):
        bounds = self.network.screenBounds()
        size = bounds.size()
        width, height = round(size.x()), round(size.y())
        # Houdini draws this entire native pane into one OpenGL Qt widget.
        # screenBounds excludes its top toolbars; screen Y starts at the bottom.
        matches = [
            w for w in [self.window] if w.objectName() == "RE_Window" and w.width() == width and w.height() >= height
        ]
        assert len(matches) == 1, dict(
            canvas=[width, height],
            window=[self.window.objectName(), self.window.metaObject().className()],
            children=[
                (w.objectName(), w.metaObject().className(), w.width(), w.height(), w.isVisible())
                for w in self.window.findChildren(QtWidgets.QWidget)
            ],
            candidates=[
                (w.objectName(), w.metaObject().className(), w.width(), w.height(), w.isVisible())
                for w in QtWidgets.QApplication.allWidgets()
                if w.width() == width or w.height() == height
            ],
        )
        point = self.network.posToScreen(self.network.itemRect(self.target).center())
        if not bounds.contains(point):
            self.frame_target()
            return
        hits = self.network.networkItemsInBox(point, point)
        assert any(
            item == self.target and name in ("node", "connectorarea", "preview", "footer") for item, name, index in hits
        ), hits
        qpoint = QtCore.QPoint(round(point.x()), matches[0].height() - round(point.y()) - 1)
        self.click_widget = matches[0]
        self.click_point = qpoint
        self.event_start = len(self.events)
        self.window.raise_()
        self.window.activateWindow()
        self.network.setIsCurrentTab()
        self.click_widget.setFocus(QtCore.Qt.OtherFocusReason)
        QtGui.QCursor.setPos(self.click_widget.mapToGlobal(qpoint))
        QtTest.QTest.mouseMove(self.click_widget, qpoint)
        self.report["last_input"] = dict(
            stage=self.stage, target=self.target.path(), screen=list(point), canvas=[width, height]
        )
        self.phase = "press"
        self.next_at = time.monotonic() + 0.2

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 110, (self.stage, self.phase, self.report)
            if time.monotonic() < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                panel_window = hou.qt.floatingPanelWindow(self.tab.floatingPanel())
                if panel_window is None:
                    return
                panel_window.move(self.window.pos() + QtCore.QPoint(820, 0))
                panel_window.show()
                self.panel.set_rop(self.fixture.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = 0
                self.phase = "wait"
                return
            if self.phase == "click":
                self.click()
                return
            if self.phase == "frame":
                self.frame_target()
                return
            if self.phase == "press":
                QtTest.QTest.mousePress(
                    self.click_widget,
                    QtCore.Qt.LeftButton,
                    QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier,
                    self.click_point,
                )
                self.phase = "release"
                self.next_at = time.monotonic() + 0.15
                return
            if self.phase == "release":
                QtTest.QTest.mouseRelease(
                    self.click_widget,
                    QtCore.Qt.LeftButton,
                    QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier,
                    self.click_point,
                )
                self.phase = "input"
                self.next_at = time.monotonic() + 0.2
                return
            if self.phase == "input":
                observed = self.events[self.event_start :]
                self.report["last_input"]["observed"] = observed
                assert any(
                    e["node"] == self.target.path() and e["ctrl"] and e["shift"] and e["left"] and not e["alt"]
                    for e in observed
                ), self.report["last_input"]
                self.phase = "wait"
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            frame = entry.session.frame_info()
            if (
                status.get("pending_update")
                or status.get("capture_pending")
                or not frame
                or not frame["complete"]
                or frame["generation"] <= self.generation
                or self.panel._live_generation != frame["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != frame["sequence"]
            ):
                return
            self.generation = frame["generation"]
            expected = [
                (0.6, 0.6, 0.6),
                (0.3, 0.3, 0.3),
                (0.2, 0.4, 0.8),
                (0.1, 0.1, 0.1),
                (0.2, 0.2, 0.2),
                (0.1, 0.1, 0.1),
                (0.2, 0.2, 0.2),
                (0.42, 0.42, 0.42),
                (0.25, 0.5, 0.75),
                (0.42, 0.42, 0.42),
                (0.2, 0.2, 0.2),
            ][self.stage]
            measured = self.fixture.expect(entry.session.frame(), expected)
            assert not status["source_diagnostics"], status["source_diagnostics"]
            if self.conversions is None:
                self.conversions = status["geometry_conversions"]
            assert status["geometry_conversions"] == self.conversions
            assert len(registry.entries()) == 1 and entry.views == 1
            if self.stage == 10:
                assert not any(n.userData("cycles_preview_output") == "1" for n in self.outer.allSubChildren())
            self.report["events"].append(
                dict(stage=self.stage, generation=self.generation, measured=measured, inputs=self.events.copy())
            )
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            self.stage += 1
            if self.stage == 11:
                self.finish()
                return
            self.prepare()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        hou.ui.removeEventLoopCallback(self.tick)
        nodegraphhooks.createEventHandler = self.original_hook
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
