# SPDX-License-Identifier: Apache-2.0
"""In the Houdini UI: a copy or paste of a protected material gets its own identity right after
the operation (deferred), and the node menu offers Keep Current Graph only where it applies.

Scenes are created in a temporary folder; nothing outside it is written.
"""

import json
import os
from pathlib import Path
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

import hou
import cycles_houdini as ch
import cycles_houdini_offline as protection

MARKER = protection.MARKER


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.folder = Path(tempfile.mkdtemp(prefix="cycles-copies-gui-"))
        self.report = dict(result="running", checks=[], folder=str(self.folder))
        self.steps = iter(self.workflow())
        # Diagnostics: when copy events arrive and when the deferred check runs.
        self.report["events"] = events = []
        child_created, after_copy = protection._child_created, protection._after_copy

        def traced_after_copy():
            events.append(("deferred check", round(time.monotonic() - self.started, 3), protection._busy))
            return after_copy()

        def traced_child_created(**kwargs):
            events.append(("child created", kwargs["child_node"].path(), protection._copy_pending))
            return child_created(**kwargs)

        protection._after_copy = traced_after_copy
        for network in (hou.node("/mat"), hou.node("/obj")):
            for types, callback in network.eventCallbacks():
                if callback is child_created:
                    network.removeEventCallback(types, callback)
        protection._child_created = traced_child_created
        self.wait_until = self.waited_from = 0.0
        self.condition = None
        self.started = time.monotonic()
        hou.ui.addEventLoopCallback(self.tick)

    def check(self, name, condition, detail=None):
        self.report["checks"].append(dict(name=name, ok=bool(condition), detail=detail))

    def save(self, name):
        hou.hipFile.save(str(self.folder / name), save_to_recent_files=False)

    def workflow(self):
        self.check("copies are watched in the UI", protection._watching())
        hou.hipFile.clear(suppress_save_prompt=True)
        original = ch.create_material(name="original")
        self.save("base.hiplc")
        first = original.userData(MARKER)
        self.report["events"].append(("copy", time.monotonic() - self.started))
        copy = hou.copyNodesTo((original,), hou.node("/mat"))[0]
        self.report["events"].append(("copied", time.monotonic() - self.started, copy.userData(MARKER) == first))
        self.check("right after the copy it still carries the original's identity", copy.userData(MARKER) == first)
        yield lambda: copy.userData(MARKER) != first
        self.check("after the copy completes: copy has its own identity", copy.userData(MARKER) not in (None, first))
        self.check("after the copy completes: original keeps its identity", original.userData(MARKER) == first)
        hou.copyNodesToClipboard((original,))
        hou.pasteNodesFromClipboard(hou.node("/mat"))
        pasted = hou.selectedNodes()[0]
        yield lambda: pasted.userData(MARKER) != first
        self.check("after a paste: pasted copy has its own identity", pasted.userData(MARKER) not in (None, first))
        box = hou.node("/obj").createNode("matnet", "box")
        inner = ch.create_material(parent=box, name="inner")
        self.save("container.hiplc")
        box_copy = hou.copyNodesTo((box,), hou.node("/obj"))[0]
        yield lambda: box_copy.node("inner").userData(MARKER) != inner.userData(MARKER)
        self.check(
            "after copying a network: nested copy has its own identity",
            box_copy.node("inner").userData(MARKER) not in (None, inner.userData(MARKER)),
        )
        self.save("copies.hiplc")
        self.check(
            "every graph has its own archive record", len(protection._read()[0]) == 5, len(protection._read()[0])
        )

        # A genuine structural conflict: the node menu offers Keep Current Graph on it only.
        hou.hipFile.clear(suppress_save_prompt=True)
        damaged = ch.create_material(name="structural_loss", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        damaged.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        self.save("intact.hiplc")
        damaged.node("surface_shader").destroy()
        protection._busy = True
        try:
            self.save("damaged.hiplc")
        finally:
            protection._busy = False
        hou.hipFile.load(str(self.folder / "damaged.hiplc"), suppress_save_prompt=True)
        damaged = hou.node("/mat/structural_loss")
        healthy = ch.create_material(name="healthy")
        menu = [
            path for path in hou.findFiles("OPmenu.xml") if "cycles_houdini_offline" in Path(path).read_text("utf-8")
        ]
        self.check("the Cycles node menu file is found", len(menu) == 1, menu)
        submenu = ET.parse(menu[0]).getroot().find("menu/subMenu")
        item = next(i for i in submenu.findall("scriptItem") if i.get("id") == "cycles_node_keep_current_graph")
        shown = submenu.find("context/expression").text.strip()
        enabled = item.find("context/expression").text.strip()
        self.check(
            "Cycles submenu shown on the damaged material",
            eval(shown, {}, dict(kwargs=dict(node=damaged))) is True,
        )
        for label, node, expected in (("damaged material", damaged, True), ("healthy material", healthy, False)):
            self.check(
                f"Keep Current Graph shown on the {label}: {expected}",
                eval(enabled, {}, dict(kwargs=dict(node=node))) is expected,
            )
        self.check("label", item.find("label").text == "Keep Current Graph")
        exec(item.find("scriptCode").text, {}, dict(kwargs=dict(node=damaged)))
        self.check("choosing it releases the graph", damaged.userData(MARKER) not in protection._unresolved)
        self.save("accepted.hiplc")
        hou.hipFile.load(str(self.folder / "accepted.hiplc"), suppress_save_prompt=True)
        self.check("after save and reload nothing waits for review", not protection._unresolved)

    def tick(self):
        # A step yields a condition: wait (at most 10 s) until the deferred check has run.
        if self.condition is not None and not self.condition() and time.monotonic() < self.wait_until:
            return
        try:
            assert time.monotonic() - self.started < 120, "timed out"
            if self.condition is not None:
                self.report.setdefault("waits", []).append(round(time.monotonic() - self.waited_from, 3))
            self.condition = next(self.steps)
            self.waited_from = time.monotonic()
            self.wait_until = self.waited_from + 10
        except StopIteration:
            failed = [c for c in self.report["checks"] if not c["ok"]]
            self.finish(dict(self.report, result="failed" if failed else "passed", failed=failed))
        except Exception:
            self.finish(dict(self.report, result="failed", error=traceback.format_exc()))

    def finish(self, report):
        hou.ui.removeEventLoopCallback(self.tick)
        self.path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True)


Workflow()
