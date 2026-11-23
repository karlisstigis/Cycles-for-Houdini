# SPDX-License-Identifier: Apache-2.0
"""One live viewer: functions, explicit-time dependencies and scoped repairs.

Programmatic HOM edits check displayed pixels and watcher-driven updates, not
physical mouse-input certification. The shared viewer harness owns cleanup.
"""

import json
import os
from pathlib import Path
import time
import hou
import numpy as np
from PySide6 import QtCore
from material_expression_fixture import Fixture
from houdini_material_ramp_override_gui_smoke import Workflow as ViewerWorkflow


class Workflow(ViewerWorkflow):
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_EXPRESSION_VIEWER") == "native"
        hou.setFps(24)
        hou.setFrame(1)
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"], heavy=True)
        self.f.rop.setCurrent(True, clear_all_selected=True)
        self.key = "rop:" + str(self.f.rop.sessionId())
        self.expected = self.f.rgb(2.4)
        self.expected_z = 0.23
        self.displacement_baseline = None
        self.height_switch = False
        self.authored = self.f.snapshot()
        self.steps = [
            ("Native fit uses the owned input on first capture", lambda: None, False, False, False),
            ("Native Local Override edits the function input", self.input_edit, False, False, False),
            (
                "Unsupported authored function omits only the surface input",
                lambda: self.function_fault(True),
                True,
                True,
                False,
            ),
            ("Expression warning survives camera reuse", lambda: self.f.camera.parm("tx").set(0.1), True, False, True),
            ("Repair restores owned numeric functions", lambda: self.function_fault(False), False, True, False),
            ("Explicit chf samples the watched external source", lambda: self.sampled("chf"), False, True, False),
            (
                "Off-frame-only external value edit updates the image",
                lambda: self.external_value(1.2),
                False,
                False,
                False,
            ),
            ("Deleted sampled source preserves independent height", self.delete_source, True, False, False),
            (
                "Deleted-source warning survives camera reuse",
                lambda: self.f.camera.parm("tx").set(0.2),
                True,
                False,
                True,
            ),
            (
                "Recreated sampled source repairs without another control edit",
                self.recreate_source,
                False,
                False,
                False,
            ),
            ("Explicit source-value repair updates the image", lambda: self.external_value(0.8), False, False, False),
            ("cht samples equivalent explicit seconds", lambda: self.sampled("cht"), False, True, False),
            ("Requested frame and seconds variables remain owned", self.variables, False, True, False),
            ("Fractional requested time changes variables immediately", lambda: self.frame(1.25), False, False, False),
            ("Requested time repair restores the original result", lambda: self.frame(1.0), False, False, False),
            (
                "Stylesheet categories have independent function values",
                lambda: self.scoped(0.6, 0.9),
                False,
                True,
                False,
            ),
            ("Surface stylesheet edit retains the scoped height", lambda: self.scoped(0.7, 0.9), False, False, False),
            (
                "Unsupported scoped function retains independent displacement",
                lambda: self.function_fault(True),
                True,
                True,
                False,
            ),
            (
                "Scoped warning survives viewport camera movement",
                lambda: self.f.camera.parm("tx").set(0.3),
                True,
                False,
                True,
            ),
            ("Scoped expression repair clears its warning", lambda: self.function_fault(False), False, True, False),
            ("Displacement stylesheet edit changes only height", lambda: self.scoped(0.7, 0.8), False, False, False),
            ("Invalid surface literal retains generic input and height", self.bad_scoped_literal, True, False, False),
            ("Surface literal repair restores its function", lambda: self.scoped(0.7, 0.8), False, False, False),
            ("Native Local Overrides restore independent fixed height", self.restore_native, False, True, False),
            (
                "Camera navigation retains repaired native controls",
                lambda: self.f.camera.parm("tx").set(0.1),
                False,
                False,
                True,
            ),
            (
                "Invalid integer metadata expression omits only its input",
                lambda: self.integer_control_fault(True),
                True,
                True,
                False,
            ),
            (
                "Integer-result warning survives camera reuse",
                lambda: self.f.camera.parm("tx").set(0.2),
                True,
                False,
                True,
            ),
            (
                "Integer control repair restores the owned input",
                lambda: self.integer_control_fault(False),
                False,
                True,
                False,
            ),
        ]
        self.scoped_surface = None
        self.index = self.generation = 0
        self.entry = self.panel = self.session = None
        self.phase = "mount"
        self.busy = self.finished = False
        self.started = self.edited_at = time.monotonic()
        self.saved_at = self.heartbeats = 0
        self.report = dict(
            result="running",
            viewer="native" if self.native else "cycles",
            scope=__doc__,
            physical_input_certification=False,
            events=[],
        )
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(self.f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setPin(True)
            self.tab.setCurrentNode(self.f.rop, pick_node=False)
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.callback = self.tick
        self.heartbeat = QtCore.QTimer(hou.qt.mainWindow())
        self.heartbeat.timeout.connect(self.beat)
        self.heartbeat.start(20)
        hou.ui.addEventLoopCallback(self.callback)
        self.persist_wait()
        if self.native:
            self.tab.startRender()

    def input_edit(self):
        self.f.local("gain", "fval").set(0.8)
        self.f.gain = 0.8
        self.expected = self.f.rgb(4.2)

    def function_fault(self, broken):
        self.f.expression('sqrt(ch("../gain"))' if broken else 'fit(ch("../gain"),0,1,1,5)')
        self.expected = self.f.rgb(
            np.sqrt(0.2)
            if broken
            else 1 + 4 * (self.scoped_surface if self.scoped_surface is not None else self.f.gain)
        )

    def sampled(self, kind):
        self.expected = self.f.sampled(kind)

    def external_value(self, high):
        source = self.f.source.parm("sampled")
        before = source.evalAtTime(hou.time())
        self.f.edit_source(high)
        assert source.evalAtTime(hou.time()) == before == 0.2
        self.expected = self.f.rgb(self.f.gain + high)

    def delete_source(self):
        self.f.delete_source()
        self.expected = self.f.rgb(0.2)

    def recreate_source(self):
        self.f.make_source()
        self.expected = self.f.rgb(self.f.gain + self.f.source_high)

    def variables(self):
        self.f.expression('10+$F+$FF+$T+ch("../gain")')
        self.frame(1.0)

    def frame(self, value):
        hou.setFrame(value)
        self.expected = self.f.rgb(10 + np.floor(value + 0.5) + value + (value - 1) / 24 + self.f.gain)

    def scoped(self, surface, displacement):
        self.scoped_surface = surface
        self.expected, height = self.f.scoped(surface, displacement)
        self.height_switch = height != self.expected_z
        self.expected_z = height

    def bad_scoped_literal(self):
        self.f.style(material={"gain": 0.3}, surface={"gain": "bad"}, displacement={"gain": 0.8})
        self.expected = self.f.rgb(2.2)

    def restore_native(self):
        self.scoped_surface = None
        self.f.native(0.8, 0.23)
        self.expected = self.f.rgb(4.2)
        self.height_switch = True
        self.expected_z = 0.23

    def integer_control_fault(self, broken):
        self.expected = self.f.integer_control_fault(broken)


def start():
    global workflow
    workflow = Workflow()


if __name__ == "__main__":
    start()
