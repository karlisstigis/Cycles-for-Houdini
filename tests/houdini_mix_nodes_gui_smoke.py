# SPDX-License-Identifier: Apache-2.0
"""One live viewer while native mix nodes and their linked Factor are edited."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from mix_nodes_fixture import Fixture, KINDS, check_layout


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.steps = [(kind, factor) for kind in KINDS for factor in (0.25, 0.75)]
        self.stage = 0
        self.generation = 0
        self.started = time.monotonic()
        self.busy = False
        self.panel = None
        self.expected = self.f.select(*self.steps[0])
        self.network = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(700, 600))
        self.network.setPwd(self.f.builder)
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(550, 600))
        self.settings.setCurrentNode(self.f.nodes[KINDS[0]])
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.report = dict(
            result="running",
            events=[],
            scope="Actual native parameter/wiring edits with network/parameter panes and one persistent worker/viewer; independently checked accepted and presented image generations. Physical mouse/monitor certification is separate.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 150, (self.stage, self.report)
            if self.panel is None:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                return
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
            ):
                return
            if (
                self.panel._live_generation != frame["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != frame["sequence"]
            ):
                return
            measured = self.f.expect(entry.session.frame(), self.expected)
            if self.stage == 0:
                self.pid = status["renderer_pid"]
                self.conversions = status["geometry_conversions"]
            assert status["renderer_pid"] == self.pid and status["geometry_conversions"] == self.conversions
            assert not status["source_diagnostics"] and status["render_device_ids"] == [
                os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
            ]
            assert len(registry.entries()) == 1 and entry.views == 1
            kind, factor = self.steps[self.stage]
            node = self.f.nodes[kind]
            self.generation = frame["generation"]
            self.report["events"].append(
                dict(kind=kind, factor=factor, generation=self.generation, measured=measured, layout=check_layout(node))
            )
            self.path.write_text(json.dumps(self.report, indent=2))
            self.stage += 1
            if self.stage == len(self.steps):
                self.finish()
                return
            kind, factor = self.steps[self.stage]
            self.expected = self.f.select(kind, factor)
            self.settings.setCurrentNode(self.f.nodes[kind])
            self.f.nodes[kind].setSelected(True, clear_all_selected=True)
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
