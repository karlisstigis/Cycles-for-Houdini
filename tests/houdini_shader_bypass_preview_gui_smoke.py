# SPDX-License-Identifier: Apache-2.0
"""One Cycles Viewer: native shader connections/flags and automatic IPR repair."""

import json, os, time, traceback
from pathlib import Path
import hou
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from shader_bypass_preview_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.fixture = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.stage = -1
        self.generation = 0
        self.baseline = None
        self.busy = False
        self.started = time.monotonic()
        self.next_at = 0
        self.report = dict(
            result="running",
            events=[],
            host_pid=os.getpid(),
            scope="Houdini node connections and flags, one live Cycles Viewer, owned displayed pixels and automatic IPR; no physical-screen capture claim.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def mutate(self):
        f = self.fixture
        if self.stage == 1:
            f.math.bypass(True)
        elif self.stage == 2:
            f.math.setNamedInput("value2", f.texture, "alpha")
            f.value.parm("value").set(0.35)
        elif self.stage == 3:
            f.math.bypass(False)
        elif self.stage == 4:
            f.texture.parm("filename").set(f.image())
        elif self.stage == 5:
            f.output.setNamedInput("surface", f.color, "color")
        elif self.stage == 6:
            f.output.setNamedInput("surface", f.range, "result")
            f.range.bypass(True)
        elif self.stage == 7:
            f.range.bypass(False)
        elif self.stage == 8:
            f.range.setDebugFlag(True)
        self.next_at = time.monotonic() + 0.15

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 100, (self.stage, self.report)
            if time.monotonic() < self.next_at:
                return
            if self.stage == -1:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.fixture.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = 0
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
                or self.panel._live_generation != frame["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != frame["sequence"]
            ):
                return
            self.generation = frame["generation"]
            expected = [
                (0.6, 0.6, 0.6),
                (0.3, 0.3, 0.3),
                (0.35, 0.35, 0.35),
                None,
                (0.2625, 0.2625, 0.2625),
                (0.2, 0.4, 0.8),
                (0.35, 0.35, 0.35),
                (0.7, 0.7, 0.7),
                (0.7, 0.7, 0.7),
            ][self.stage]
            issues = status["source_diagnostics"]
            assert bool(issues) == (self.stage == 3), (self.stage, issues)
            pixels = entry.session.frame()
            measured = self.fixture.expect(pixels, expected) if expected else self.fixture.measure(pixels).tolist()
            if self.baseline is None:
                self.baseline = status["geometry_conversions"]
                self.worker = status.get(
                    "renderer_pid", entry.session.renderer_pid if hasattr(entry.session, "renderer_pid") else None
                )
            assert status["geometry_conversions"] == self.baseline
            assert len(registry.entries()) == 1 and entry.views == 1
            assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            self.report["events"].append(
                dict(stage=self.stage, generation=self.generation, measured=measured, diagnostics=issues, status=status)
            )
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            self.stage += 1
            if self.stage == 9:
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                self.finish()
                return
            self.mutate()
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
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
