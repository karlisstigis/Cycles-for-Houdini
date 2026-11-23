# SPDX-License-Identifier: Apache-2.0
"""Artist actions using Houdini's native ROP and floating-panel lifecycles."""

import os
import time
from contextlib import ExitStack

import hou

from . import final_render

_ipr_window = None


def _focus(window):
    if window.isMinimized():
        window.showNormal()
    window.raise_()
    window.activateWindow()


def open_ipr(rop):
    """Focus an existing floating view, or build a hidden window before showing it."""
    if not hou.isUIAvailable():
        raise RuntimeError("IPR requires Houdini's graphical interface")
    from PySide6 import QtCore, QtWidgets
    from shiboken6 import isValid
    from ..viewer.panel import RenderPanel

    global _ipr_window
    if _ipr_window is not None and isValid(_ipr_window):
        _focus(_ipr_window)
        return _ipr_window
    for floating in hou.ui.floatingPanels():
        for pane in floating.paneTabs():
            if pane.type() == hou.paneTabType.PythonPanel:
                interface = pane.activeInterface()
                if interface and interface.name() == "cycles_houdini_render":
                    pane.setIsCurrentTab()
                    window = hou.qt.floatingPanelWindow(floating)
                    _focus(window)
                    return window

    class IPRWindow(QtWidgets.QDialog):
        def __init__(self):
            super().__init__(hou.qt.mainWindow(), QtCore.Qt.WindowType.Window)
            self.setWindowTitle("Cycles Render View")
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
            self.resize(1100, 800)
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.panel = RenderPanel(self)
            layout.addWidget(self.panel)
            self.panel.set_rop(rop)

        def closeEvent(self, event):
            self.panel.dispose()
            super().closeEvent(event)

        def done(self, result):
            self.panel.dispose()
            super().done(result)

    window = IPRWindow()
    _ipr_window = window

    def forget(*_):
        global _ipr_window
        if _ipr_window is window:
            _ipr_window = None

    window.destroyed.connect(forget)
    # Qt creates the entire widget tree while this window is hidden. Unlike a
    # native floating Python Panel, no empty container is mapped beforehand.
    window.show()
    window.panel.render()
    _focus(window)
    return window


class MPlayOutput:
    def __init__(self, rop):
        # Independent camera sessions avoid MPlay's new-render-slot transition
        # while another source still has incoming tiles.
        self.label = f"cycles_{os.getpid()}_{rop.sessionId()}"
        self.rop = rop
        self.connected = False

    def prepare(self, session, at_time):
        rop = self.rop
        if rop.parm("trange").eval():
            start, end, _ = rop.parmTuple("f").eval()
        else:
            start = end = hou.timeToFrame(at_time)
        options = {"reuse_only": "1"} if self.connected else {}
        session.open_native_display(
            options={
                "label": self.label,
                "currentframe": f"{hou.timeToFrame(at_time):.17g}",
                "framerange": f"{start:.17g} {end:.17g}",
                "rendermode": "replace",
                "rendersource": rop.path(),
                **options,
            }
        )
        session.set_feedback("native-display", True)

    def _display_call(self, callback):
        from .. import _native

        try:
            return callback()
        except _native.DisplayRemoteQuit as error:
            raise final_render.RenderCancelled("MPlay render stopped") from error
        except _native.DisplayConnectionError as error:
            if self.connected:
                raise final_render.RenderCancelled("MPlay disconnected") from error
            raise

    def _ready(self, session):
        ready = self._display_call(session.native_display_ready)
        self.connected = self.connected or ready
        return ready

    def progress(self, session, frame, generation):
        # Poll even before the first pixels so viewer Stop is observed during
        # rendering. The native writer coalesces updates within this frame.
        self._ready(session)
        if frame and frame["generation"] == generation:
            self._display_call(session.present_native)

    def present(self, session, *, before_write):
        self._display_call(session.present_native)
        deadline = time.monotonic() + 30
        with hou.InterruptableOperation("Sending frame to MPlay", open_interrupt_dialog=True) as operation:
            while not self._ready(session):
                before_write()
                operation.updateProgress(0)
                if time.monotonic() >= deadline:
                    raise TimeoutError("MPlay did not accept the rendered frame within 30 seconds")
                time.sleep(0.01)

    def report_gpu_failure(self, session, message):
        from .. import _native

        set_status = getattr(_native, "set_native_display_status", None)
        if set_status is not None:
            try:
                if set_status(session._handle, message):
                    return
            except RuntimeError:
                pass  # The connection can close before the writer accepts the warning.
        hou.ui.setStatusMessage(message, severity=hou.severityType.Error)


def render_mplay(rop):
    """Render the Cycles dependency chain to MPlay and each ROP's Output Picture."""
    if not hou.isUIAvailable():
        raise RuntimeError("Render to MPlay requires Houdini's graphical interface")
    dependencies = [node for node, frames in rop.inputDependencies()]
    nodes = {node.sessionId(): node for node in (*dependencies, rop) if node.type().name() == "cycles_ipr"}
    cancelled = []
    failures = []
    gpu_failed = False

    def observe(event, value):
        nonlocal gpu_failed
        if event == "sequence":
            node, summary = value
            if node.sessionId() in nodes and summary.get("cancelled") and summary.get("reason") == "cancelled":
                cancelled.append(node.path())
            elif node.sessionId() in nodes and summary.get("error") and summary.get("reason") == "failed":
                failures.append(f"{node.path()}: {summary['error']}")
                gpu_failed |= summary.get("gpu_failure") is not None

    try:
        with final_render.observe(observe), ExitStack() as stack:
            for node in nodes.values():
                stack.enter_context(final_render.output_to(node, MPlayOutput(node)))
            rop.render()
    except (hou.OperationFailed, hou.OperationInterrupted) as error:
        if not cancelled and not isinstance(error, hou.OperationInterrupted):
            if gpu_failed:
                # SOHO stopped the dependency chain and the output posted the
                # warning. Keep the action failure out of the callback traceback.
                return False
            if failures:
                raise hou.OperationFailed(failures[-1]) from error
            raise
        hou.ui.setStatusMessage("Cycles render cancelled", severity=hou.severityType.Message)
        return False
    return not gpu_failed
