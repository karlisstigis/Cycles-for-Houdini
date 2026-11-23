# SPDX-License-Identifier: Apache-2.0
"""A completed op: render stays idle, while source edits and repairs still update."""

from collections import deque
import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import _native, native_scene, registry
from shader_bypass_preview_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.motion = os.environ.get("CYCLES_HOUDINI_COP_IDLE_MOTION") == "1"
        if self.motion:
            self.f.rop.parm("allowmotionblur").set(1)
            self.f.camera.parm("shutter").set(0.5)
        self.events = deque(maxlen=160)
        self.original_changed = native_scene.SceneObserver._changed
        workflow = self

        def changed(observer, **kwargs):
            node = kwargs.get("node")
            parm = kwargs.get("parm_tuple")
            workflow.events.append(
                dict(
                    time=time.monotonic(),
                    node=node.path() if node else None,
                    event=str(kwargs.get("event_type")),
                    parm=parm.name() if parm is not None else None,
                    flags=str(kwargs.get("flags", kwargs.get("flag", ""))),
                )
            )
            return workflow.original_changed(observer, **kwargs)

        native_scene.SceneObserver._changed = changed
        self.sources = []
        legacy = hou.node("/img").createNode("img", "idle_global_legacy")
        self.sources.append((legacy, "color", "color", (0.25, 0.5, 0.75, 1)))
        types = self.f.geo.childTypeCategory().nodeTypes()
        for kind in ("cop2net", "copnet"):
            if kind not in types:
                continue
            network = self.f.geo.createNode(kind, "idle_" + kind)
            child_types = network.childTypeCategory().nodeTypes()
            if "constant" in child_types:
                network.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
                self.sources.append((network, "constant", "f3", (0.25, 0.5, 0.75)))
            elif "color" in child_types:
                self.sources.append((network, "color", "color", (0.25, 0.5, 0.75, 1)))
            else:
                raise AssertionError((kind, sorted(child_types)))
        self.case = self.edit = 0
        self.color = self.make_color()
        self.bind()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.phase = "mount"
        self.started = time.monotonic()
        self.idle_since = None
        self.anchor = None
        self.generation = 0
        self.busy = False
        self.report = dict(
            result="running",
            states=[],
            motion_blur=self.motion,
            scope="One live Cycles Viewer, idle capture counters, independent pixels, COP source edits and deletion/recreation",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def make_color(self):
        parent, kind, field, value = self.sources[self.case]
        node = parent.createNode(kind, "color")
        if kind == "constant":
            node.parm("signature").set("f3")
        else:
            node.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
        node.parmTuple(field).set(value)
        if self.motion:
            node.parm(field + "r").setExpression(str(value[0]) + "+0*$F", hou.exprLanguage.Hscript)
        return node

    def bind(self):
        self.f.texture.setParms(dict(filename="op:" + self.color.path(), colorspace="data", alpha_type="ignore"))
        self.f.output.setNamedInput("surface", self.f.texture, "color")

    def counters(self, status):
        entry = self.panel.entry
        return dict(
            generation=status["generation"],
            revision=status["render_revision"],
            change_serial=entry.change_serial,
            geometry_conversions=status["geometry_conversions"],
            cop=_native.cop_capture_stats(entry.session._handle),
        )

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            assert now - self.started < 180, (self.case, self.edit, self.phase)
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if self.phase == "idle":
                current = self.counters(status)
                assert current == self.anchor, dict(
                    reason="Unedited completed render queued or captured again",
                    case=self.sources[self.case][0].path(),
                    edit=self.edit,
                    before=self.anchor,
                    after=current,
                    status=status,
                    events=list(self.events),
                )
                if now - self.idle_since < 4:
                    return
                self.report["states"].append(
                    dict(
                        source=self.sources[self.case][0].path(),
                        edit=self.edit,
                        idle_seconds=now - self.idle_since,
                        **current,
                    )
                )
                self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
                self.generation = status["generation"]
                self.edit += 1
                self.phase = "wait"
                self.idle_since = self.anchor = None
                if self.edit == 1:
                    field = self.sources[self.case][2]
                    value = list(self.sources[self.case][3])
                    value[0] = 0.625
                    self.color.parmTuple(field).set(value)
                    if self.motion:
                        self.color.parm(field + "r").setExpression(".625+0*$F", hou.exprLanguage.Hscript)
                elif self.edit == 2:
                    self.color.destroy()
                elif self.edit == 3:
                    self.color = self.make_color()
                else:
                    self.case += 1
                    if self.case == len(self.sources):
                        self.finish()
                        return
                    self.edit = 0
                    self.color = self.make_color()
                    self.bind()
                return
            frame = entry.session.frame_info()
            if (
                status["state"] != "complete"
                or status.get("pending_update")
                or status.get("capture_pending")
                or not frame
                or not frame["complete"]
                or status["generation"] <= self.generation
            ):
                return
            if self.panel._live_generation != frame["generation"]:
                return
            expected = (1, 0, 1) if self.edit == 2 else (0.625, 0.5, 0.75) if self.edit == 1 else (0.25, 0.5, 0.75)
            self.f.expect(entry.session.frame(), expected)
            assert bool(status["source_diagnostics"]) == (self.edit == 2), status["source_diagnostics"]
            self.anchor = self.counters(status)
            self.idle_since = now
            self.phase = "idle"
        except Exception:
            self.report["events"] = list(self.events)
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        native_scene.SceneObserver._changed = self.original_changed
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
