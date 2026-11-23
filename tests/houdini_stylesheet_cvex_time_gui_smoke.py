# SPDX-License-Identifier: Apache-2.0
"""Actual channel edits and repair in one IPR viewer with a heavy shared prototype."""

import json, traceback
import hou, numpy as np
from houdini_material_stylesheet_gui_smoke import Workflow as Base, sheet


class Workflow(Base):
    def __init__(self):
        super().__init__()
        f = self.f
        self.channel = hou.node("/obj").createNode("null", "live_cvex_channel")
        self.channel.addSpareParmTuple(hou.FloatParmTemplate("gain", "Gain", 1))
        self.gain = self.channel.parm("gain")
        self.gain.set(2)
        self.settings.setCurrentNode(self.channel)
        self.steps = [
            ("Underlying material", lambda: None, 1, False),
            ("Read unrelated channel", lambda: self.apply(), 2, False),
            ("Edit unrelated channel", lambda: self.gain.set(4), 4, False),
            ("Edit channel again", lambda: self.gain.set(3), 3, False),
            ("Channel creates invalid result", lambda: (self.apply(True), self.gain.set(1000)), 1, True),
            ("Repair channel result", lambda: self.gain.set(0), 1, False),
            ("Repair changes result", lambda: self.gain.set(1), np.e, False),
            ("Return to direct channel", lambda: (self.gain.set(2), self.apply()), 2, False),
            ("Animated channel frame one", lambda: self.gain.setExpression("$F+1"), 2, False),
            ("Animated channel frame three", lambda: hou.setFrame(3), 4, False),
            ("Animated channel frame five", lambda: hou.setFrame(5), 6, False),
            ("Restore frame one", lambda: hou.setFrame(1), 2, False),
            ("Object self rule", lambda: self.apply(False, True), 2, False),
            ("Edit object dependency", lambda: (self.gain.deleteAllKeyframes(), self.gain.set(3)), 3, False),
            (
                "Broken script keeps geometry",
                lambda: self.control.set(
                    sheet(
                        {"materialParameters": {"power": {"script": {"code": "cvex broken export float result=0){}"}}}}
                    )
                ),
                1,
                True,
            ),
            ("Repair script channel", lambda: self.apply(), 3, False),
            ("Camera navigation", lambda: f.camera.parm("tx").set(0.3), 3, False),
            ("Packed object transform", lambda: f.heavy.parm("tx").set(0.5), 3, False),
            ("Remove channel rule", lambda: self.control.set(""), 1, False),
        ]
        self.steps = [
            (label, action, tuple(np.array((0.2, 0.4, 0.6)) * power), warning)
            for label, action, power, warning in self.steps
        ]
        self.report["scope"] = (
            "Implicit CVEX channel time, external dependency observation and live result repair; one heavy packed prototype remains resident"
        )

    def apply(self, exponential=False, self_target=False):
        value = 'chf("' + self.gain.path() + '")'
        if exponential:
            value = "exp(" + value + ")"
        script = {"script": {"code": "cvex clock(export float result=0){result=" + value + ";}", "return": "result"}}
        self.control.set(sheet({"materialParameters": {"power": script}}, {"type": "self"} if self_target else {}))


def launch():
    global workflow
    try:
        workflow = Workflow()
    except BaseException:
        traceback.print_exc()
        hou.exit(1, suppress_save_prompt=True)


hou.ui.postEventCallback(launch)
