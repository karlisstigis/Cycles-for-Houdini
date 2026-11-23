# SPDX-License-Identifier: Apache-2.0
"""One worker/viewer: live edits, native ROP settings and Stop/Render intent."""

import os
from pathlib import Path
import runpy
import time
import traceback
import faulthandler

import hou
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry


os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "65536"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="ownership_fixture")[
    "HeavyResponse"
]


class OwnershipWorkflow(HeavyResponse):
    def __init__(self, native=False):
        self.native = native
        self.create_render_panel = not native
        super().__init__()
        self.trace_file = self.path.with_suffix(".threads.log").open("w")
        faulthandler.dump_traceback_later(45, repeat=True, file=self.trace_file)
        self.report.update(
            render_viewers=["native" if native else "cycles"],
            scope="One persistent worker on the million-triangle SOP courtyard. Camera update, final-only memory control, native thread-count field, resource replacement and Stop/Render. No speed certification.",
        )
        if native:
            self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.viewer.setRopNode(self.rop)
            self.viewer.setPreview(True)
            self.viewer.setAutoUpdate(True)
            self.viewer.setDelay(0.02)
            self.viewer.startRender()

    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.parameters = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
        self.parameters.setCurrentNode(self.rop)
        self.parameters.setPin(True)
        self.events = []
        self.pipeline = None

    def ready(self, entry):
        if entry.error:
            raise RuntimeError(entry.error)
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if not frame or frame["generation"] != status["generation"] or status.get("pending_update"):
            return False
        assert entry.session.execution == "worker" and status["renderer_pid"] != os.getpid()
        assert len(registry.entries()) == 1 and entry.views == (0 if self.native else 1)
        if self.native:
            return (
                entry.helper_pid
                and self.viewer.isActive()
                and self.viewer.imageResolution() == (960, 540)
                and "C" in self.viewer.planes()
            )
        self.panel.poll()
        return self.panel._session is entry.session and self.panel.canvas.generation == frame["generation"]

    def next(self, stage, delay=0.1):
        self.stage = stage
        self.report["phase"] = stage
        self.next_at = time.monotonic() + delay

    def record(self, name, entry):
        self.events.append(
            dict(
                name=name,
                status=entry.session.status(),
                helper_pid=entry.helper_pid,
                channel=entry.channel,
                render_threads=self.rop.evalParm("render_threads"),
            )
        )
        self.report["events"] = self.events
        self.save()

    def finish_owned(self, entry, failed=False):
        self.report["cleanup_stage"] = "begin"
        self.save()
        self.timer.stop()
        hou.ui.removeEventLoopCallback(self.callback)
        registry.Entry.apply = self.original_apply
        registry.session_from_configuration = self.original_factory
        if self.viewer:
            self.report["cleanup_stage"] = "kill native viewer"
            self.save()
            self.viewer.killRender()
            self.viewer.close()
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        # Production close is nonblocking; this final resource assertion runs
        # only after explicitly draining retirement in the test teardown.
        entry.session.shutdown()
        self.report["cleanup_stage"] = "registry closed"
        self.save()
        if not failed:
            closed = entry.session.status()
            assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
            self.report.update(
                result="passed",
                phase="complete",
                closed=closed,
                events=self.events,
                wall_seconds=time.monotonic() - self.began,
            )
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if failed else 0, suppress_save_prompt=True))

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        entry = registry.get(self.key)
        try:
            if time.monotonic() - self.began > 175:
                raise TimeoutError("Worker ownership workflow stalled")
            if time.monotonic() < self.next_at or entry is None:
                return
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                assert self.rop.parm("ipr_execution") is None and self.rop.parm("final_execution") is None
                self.original = entry.session
                self.baseline = entry.session.status()
                if self.native:
                    self.pipeline = (entry.channel, entry.socket_port, entry.helper_pid)
                self.record("IPR starts a worker without a mode selector", entry)
                self.camera.parm("tx").set(self.camera_x + 0.1)
                self.next("camera")
                return
            if self.stage == "camera":
                if not self.ready(entry) or entry.session.status()["generation"] <= self.baseline["generation"]:
                    return
                status = entry.session.status()
                assert entry.session is self.original and status["renderer_pid"] == self.baseline["renderer_pid"]
                assert status["geometry_conversions"] == self.baseline["geometry_conversions"]
                self.record("Camera updates retain the worker and GPU geometry", entry)
                self.serial = entry.change_serial
                self.generation = status["generation"]
                self.applies = len(self.report["applies"])
                self.rop.parm("persistent_data").set(0)
                self.next("final-memory", 0.7)
                return
            if self.stage == "final-memory":
                assert entry.session.status()["generation"] == self.generation and entry.change_serial == self.serial, (
                    dict(
                        status=entry.session.status(),
                        serial=entry.change_serial,
                        expected_serial=self.serial,
                        soho=entry.last_soho,
                    )
                )
                assert len(self.report["applies"]) == self.applies
                self.record("Final Persistent Data does not disturb IPR", entry)
                window = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
                window.show()
                window.raise_()
                window.activateWindow()
                self.parameters.setIsCurrentTab()
                parm = self.rop.parm("render_threads")
                # moveFocusTo does not open an inactive native parameter tab.
                # Select and scroll through public HOM before typing the field.
                folder = parm.containingFolderSetParmTuples()[0]
                folder[0].set(parm.containingFolderIndices()[0])
                self.next("native-open", 0.5)
                return
            if self.stage == "native-open":
                window = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
                picture = window.screen().grabWindow(int(window.winId()))
                picture.save(str(self.path.with_suffix(".threads-before.png")))
                # The second header on the observed native Performance tab is
                # Threads. As in the native world/handle workflow, send its
                # input to RE_Window rather than the OpenGL drawing child.
                ratio = picture.devicePixelRatio()
                point = QtCore.QPoint(round(27 / ratio), round(242 / ratio))
                target = window.childAt(point)
                while target is not None and target.objectName() != "RE_Window":
                    target = target.parentWidget()
                assert target is not None
                local = target.mapFrom(window, point)
                global_point = window.mapToGlobal(point)
                QtGui.QCursor.setPos(global_point)
                for kind, button, buttons in (
                    (QtCore.QEvent.MouseMove, QtCore.Qt.NoButton, QtCore.Qt.NoButton),
                    (QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton),
                    (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.LeftButton, QtCore.Qt.NoButton),
                ):
                    event = QtGui.QMouseEvent(
                        kind, QtCore.QPointF(local), QtCore.QPointF(global_point), button, buttons, QtCore.Qt.NoModifier
                    )
                    QtWidgets.QApplication.sendEvent(target, event)
                self.next("native-focus", 0.5)
                return
            if self.stage == "native-focus":
                parm = self.rop.parm("render_threads")
                self.parameters.scrollTo(parm, hou.scrollPosition.Top)
                self.parameters.moveFocusTo(parm)
                self.next("native-field", 0.5)
                return
            if self.stage == "native-field":
                assert self.rop.parmTuple("render_threads") in self.parameters.visibleParms()
                window = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
                focused = QtWidgets.QApplication.focusWidget()
                widget = (
                    focused if focused is not None and (focused is window or window.isAncestorOf(focused)) else window
                )
                self.report["native_focus"] = dict(
                    class_name=widget.metaObject().className(),
                    object_name=widget.objectName(),
                    owned=widget is window or window.isAncestorOf(widget),
                )
                QtTest.QTest.keyClick(widget, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(widget, "1")
                QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Return)
                # Houdini commits native key input after this host callback
                # returns. Do not read the node value in the delivery callback.
                self.next("typed-native-field", 0.5)
                return
            if self.stage == "typed-native-field":
                assert self.rop.evalParm("render_threads") == 1, self.rop.evalParm("render_threads")
                self.next("replacement")
                return
            if self.stage == "replacement":
                if entry.session is self.original or not self.ready(entry):
                    return
                closed = self.original.status()
                assert (
                    closed["state"] == "closed"
                    and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
                )
                if self.native:
                    assert (entry.channel, entry.socket_port, entry.helper_pid) == self.pipeline
                self.replacement = entry.session
                self.record("Native thread setting replaces the worker and retains the host pipe", entry)
                window = hou.qt.floatingPanelWindow((self.viewer if self.native else self.tab).floatingPanel())
                assert window.screen().grabWindow(int(window.winId())).save(str(self.path.with_suffix(".viewer.png")))
                parameters = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
                assert (
                    parameters.screen()
                    .grabWindow(int(parameters.winId()))
                    .save(str(self.path.with_suffix(".parameters.png")))
                )
                if self.native:
                    self.viewer.pauseRender()
                    self.next("paused")
                    return
                assert self.panel.stop_button.isEnabled()
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.next("stopped")
                return
            if self.stage == "paused":
                if entry.session.status()["state"] != "paused":
                    return
                self.viewer.resumeRender()
                self.next("resumed")
                return
            if self.stage == "resumed":
                if entry.session.status()["state"] != "rendering":
                    return
                assert (
                    entry.session is self.replacement
                    and (entry.channel, entry.socket_port, entry.helper_pid) == self.pipeline
                )
                self.record("Native Pause/Resume retains the same worker and pipe", entry)
                self.finish_owned(entry)
                return
            if self.stage == "stopped":
                if entry.session.status()["state"] != "cancelled":
                    return
                self.camera.parm("tx").set(self.camera_x + 0.2)
                self.next("stopped-edit", 0.5)
                return
            if self.stage == "stopped-edit":
                assert entry.session is self.replacement and entry.session.status()["state"] == "cancelled"
                assert (
                    self.panel.render_button.isEnabled()
                    and not self.panel.pause_button.isEnabled()
                    and not self.panel.stop_button.isEnabled()
                )
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.next("restarted")
                return
            if self.stage == "restarted":
                if not self.ready(entry) or entry.session.status()["state"] != "rendering":
                    return
                assert entry.session is self.replacement
                self.record("Stop, edit and Render reuse the persistent worker", entry)
                self.finish_owned(entry)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc(), phase=self.stage)
            parameters = hou.qt.floatingPanelWindow(self.parameters.floatingPanel())
            parameters.screen().grabWindow(int(parameters.winId())).save(str(self.path.with_suffix(".failure.png")))
            self.finish_owned(entry, failed=True)
        finally:
            self.tick_busy = False


if __name__ == "__main__":
    workflow = OwnershipWorkflow()
