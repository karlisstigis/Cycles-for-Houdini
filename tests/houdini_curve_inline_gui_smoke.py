# SPDX-License-Identifier: Apache-2.0
"""Inline curve ramps in the parameter pane: Float/RGB/Vector Curves show native
float ramp gadgets (Value first, Factor last), no Edit Curves button or editor
window opens, and Float Ramp is gone from the node catalogue and Tab menu."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtWidgets
import cycles_houdini as ch

CHANNELS = {"float_curve": ("value",), "rgb_curves": ("c", "r", "g", "b"), "vector_curves": ("x", "y", "z")}


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", scope=__doc__, checks=[])
        self.material = ch.create_material(name="inline_curves", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        self.nodes = {kind: self.material.createNode("cycles::" + kind + "::1", kind) for kind in CHANNELS}
        self.queue = list(self.nodes.items())
        self.pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(520, 760))
        self.pane.setPin(True)
        self.current = None
        self.due = time.monotonic() + 0.5
        self.began = time.monotonic()
        self.busy = False
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def check(self, label, **data):
        self.report["checks"].append(dict(label=label, **data))

    def windows(self):
        return [w.windowTitle() for w in QtWidgets.QApplication.topLevelWidgets() if w.isVisible()]

    def inspect(self, kind, node):
        assert node.parm("edit_curves") is None, "Edit Curves button still present"
        assert node.inputNames() == ("value", "fac"), node.inputNames()
        entries = [t.name() for t in node.parmTemplateGroup().entries()]
        assert entries[0] == "value" and entries[-1] == "fac", entries
        for channel in CHANNELS[kind]:
            parm = node.parm("curve_" + channel)
            template = parm.parmTemplate()
            assert template.type() == hou.parmTemplateType.Ramp and template.parmType() == hou.rampParmType.Float, (
                kind,
                channel,
            )
            assert not template.isHidden() and not parm.isHidden(), (kind, channel)
            assert hou.parmCondType.HideWhen not in template.conditionals(), template.conditionals()
        # Pressing nothing opens nothing: no editor window among top levels.
        titles = self.windows()
        assert not any("Curve" in title or "curve" in title for title in titles), titles
        self.check(
            "Inline native float ramps, Value first and Factor last; no editor",
            kind=kind,
            parameters=entries,
            top_level_windows=titles,
        )

    def catalogue(self):
        category = hou.vopNodeTypeCategory()
        assert hou.nodeType(category, "cycles::float_ramp::1") is None, "Float Ramp is still registered"
        for kind in CHANNELS:
            node_type = hou.nodeType(category, "cycles::" + kind + "::1")
            assert node_type is not None and not node_type.hidden(), kind
        tools = [name for name in hou.shelves.tools() if "float_ramp" in name.lower() and "cycles" in name.lower()]
        assert not tools, tools
        curve_tools = [
            name for name in hou.shelves.tools() if "cycles" in name.lower() and "float_curve" in name.lower()
        ]
        self.check("Float Ramp absent from node types and Tab-menu tools; curve nodes visible", curve_tools=curve_tools)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def finish(self, error=None):
        self.report.update(result="failed" if error else "passed", error=error)
        self.save()
        hou.ui.removeEventLoopCallback(self.callback)
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))

    def tick(self):
        if self.busy or time.monotonic() < self.due:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 90
            if self.current is not None:
                kind, node = self.current
                self.inspect(kind, node)
                try:
                    widget = next(
                        w
                        for w in QtWidgets.QApplication.topLevelWidgets()
                        if w.isVisible() and w.width() >= 400 and w.height() >= 600
                    )
                    widget.grab().save(str(self.path.parent / f"{kind}-parameters.png"))
                except StopIteration:
                    pass
            if not self.queue:
                self.catalogue()
                self.finish()
                return
            self.current = self.queue.pop(0)
            self.pane.setCurrentNode(self.current[1])
            self.due = time.monotonic() + 0.6
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False


if __name__ == "__main__":
    workflow = Workflow()
