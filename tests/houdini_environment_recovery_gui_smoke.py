# SPDX-License-Identifier: Apache-2.0
"""Native parameter edits and live environment repair in one Cycles Viewer."""

import json
import os
import time
import traceback
from pathlib import Path

import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from cycles_houdini import light_properties, registry
from environment_recovery_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 550))
        self.settings.setPin(True)
        f = self.f
        self.states = [
            (None, None, 1, (), False),
            (f.env.parm("env_filteramount"), 0.5, 1, ("env_filteramount",), False),
            (f.env.parm("light_intensity"), 2, 2, ("env_filteramount",), False),
            (f.env.parm("env_filteramount"), 0, 2, (), False),
            (f.env.parm("shadow_intensity"), 0.25, 2, ("shadow_intensity",), False),
            (f.env.parm("shadow_intensity"), 1, 2, (), False),
            (f.env.parm("shadowmask"), "no_matching_object", 2, ("shadowmask",), False),
            (f.env.parm("shadowmask"), "*", 2, (), False),
            (f.env.parm("env_map"), str(f.dest / "missing-gui-world.exr"), 0, (), True),
            (f.env.parm("env_map"), str(f.image), 2, (), False),
            (f.env.parm("light_intensity"), 3, 3, ("env_portalenable",), False),
            (f.camera.parm("tx"), 0.2, 3, ("env_portalenable",), False),
            (f.geo.parm("tx"), 0.1, 3, ("env_portalenable",), False),
            (f.env.parm("light_intensity"), 1, 1, (), False),
            (f.env.parm("light_exposure"), 2048, 0, (), True),
            (f.env.parm("light_exposure"), 0, 1, (), False),
            (f.camera.parm("tx"), 0.05, 1, ("light_contrib",), False),
            (f.env.parm("light_intensity"), 1.5, 1.5, (), False),
            (None, None, 0, (), False),  # Delete the environment while IPR remains enabled.
            (None, 2, 2, (), False),  # Recreate the same path, then edit its actual Intensity.
            (None, 0.25, 2, ("shadow_intensity",), False),
            (None, 1, 2, (), False),
        ]
        self.panel = None
        self.index = 0
        self.phase = "mount"
        self.started = time.monotonic()
        self.next_at = 0
        self.last_report = 0
        self.busy = False
        self.report = dict(
            result="running",
            events=[],
            scope="Native parameter keyboard edits, one retained worker/viewer, accepted scene diagnostics and displayed frame metadata plus independent raw pixels. Physical display and production latency are separate.",
        )
        hou.ui.addEventLoopCallback(self.tick)

    def edit(self):
        f = self.f
        if self.index == 18:
            self.before_serial = self.panel.entry.change_serial
            f.env.destroy()
            self.parm = None
            self.phase = "wait"
            self.next_at = time.monotonic() + 0.2
            return
        if self.index == 19:
            f.env = f.scene.createNode("envlight", "environment")
            light_properties.install(f.env)
            f.env.setParms(
                dict(
                    env_map=str(f.image),
                    light_contribprimary=1,
                    light_intensity=1,
                    light_exposure=0,
                    cycles_environment_colorspace="data",
                )
            )
            f.env.parmTuple("light_color").set((1, 1, 1))
        if self.index == 10:
            f.env.parm("env_portalenable").set(1)
        elif self.index == 13:
            f.env.parm("env_portalenable").set(0)
        elif self.index == 16:
            f.env.parm("light_contrib").set(1)
        elif self.index == 17:
            f.env.parm("light_contrib").set(0)
        self.parm, value, _, _, _ = self.states[self.index]
        if self.index >= 19:
            self.parm = f.env.parm("light_intensity" if self.index == 19 else "shadow_intensity")
        self.text = str(value)
        self.before_serial = self.panel.entry.change_serial
        self.settings.setCurrentNode(self.parm.node())
        self.settings.setIsCurrentTab()
        for folder, index in zip(self.parm.containingFolderSetParmTuples(), self.parm.containingFolderIndices()):
            template = self.parm.node().parmTemplateGroup().find(folder.name())
            folder[0].set(
                1
                if isinstance(template, hou.FolderParmTemplate) and template.folderType() == hou.folderType.Collapsible
                else index
            )
        self.window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setFocus(QtCore.Qt.OtherFocusReason)
        QtGui.QCursor.setPos(self.window.mapToGlobal(self.window.rect().center()))
        self.phase = "focus"
        self.next_at = time.monotonic() + 0.25

    def ready(self):
        entry = self.panel.entry
        if entry is None:
            return False
        status = entry.session.status()
        assert not entry.error and status["state"] != "failed", (entry.error, status)
        info = entry.session.frame_info()
        if time.monotonic() - self.last_report > 0.25:
            self.last_report = time.monotonic()
            self.report["current"] = dict(
                index=self.index,
                phase=self.phase,
                serial=entry.change_serial,
                applied=entry.applied_serial,
                before_serial=getattr(self, "before_serial", None),
                capture_pending=status.get("capture_pending"),
                pending_update=status.get("pending_update"),
                state=status["state"],
                generation=status["generation"],
                frame=info,
                live_generation=self.panel._live_generation,
                canvas_sequence=getattr(self.panel.canvas, "frame_metadata", {}).get("sequence"),
                diagnostics=status["source_diagnostics"],
                camera_tx=self.f.camera.evalParm("tx"),
                contributions=self.f.env.evalParm("light_contrib") if self.index != 18 else None,
            )
            self.path.write_text(json.dumps(self.report, indent=2))
        if self.index and (entry.change_serial <= self.before_serial or entry.applied_serial != entry.change_serial):
            return False
        if status.get("capture_pending") or status.get("pending_update") or not info or not info["complete"]:
            return False
        # An ignored option may update diagnostics while retaining the same
        # complete image. Require acknowledgement, not an artificial render restart.
        if (
            self.panel._live_generation != info["generation"]
            or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
        ):
            return False
        _, _, scale, settings, unavailable = self.states[self.index]
        issues = status["source_diagnostics"]
        optional = [issue for issue in issues if issue["code"] == "world.unsupported_setting"]
        if (
            len(optional) != len(settings)
            or bool([i for i in issues if i["code"] == "world.unavailable"]) != unavailable
        ):
            return False
        if not all(any(name in i["message"] for i in optional) for name in settings):
            return False
        expected = self.f.fallback if unavailable or self.index == 18 else self.f.radiance * scale
        planes = self.f.check(entry.session.frame(), status, expected, settings, unavailable=unavailable)
        if self.index == 12:
            assert np.mean(np.abs(planes["position"] - self.previous_position)) > 0.001
        self.previous_position = planes["position"]
        assert len(registry.entries()) == 1 and entry.views == 1
        assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        if self.index == 0:
            self.pid = status["renderer_pid"]
            self.conversions = status["geometry_conversions"]
        assert status["renderer_pid"] == self.pid and status["geometry_conversions"] == self.conversions
        assert self.pid != os.getpid()
        self.report["events"].append(
            dict(
                index=self.index,
                parameter=self.parm.path() if self.index and self.parm else None,
                generation=info["generation"],
                sequence=info["sequence"],
                diagnostics=issues,
                radiance=planes["combined"][2, 2, :3].tolist(),
                geometry_conversions=status["geometry_conversions"],
            )
        )
        self.path.write_text(json.dumps(self.report, indent=2))
        return True

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 220, (self.index, self.phase, self.report.get("current"))
            if time.monotonic() < self.next_at:
                return
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                return
            if self.phase == "focus":
                self.settings.moveFocusTo(self.parm.tuple().name())
                self.phase = "keys"
                self.next_at = time.monotonic() + 0.2
                return
            if self.phase == "keys":
                focus = QtWidgets.QApplication.focusWidget() or self.window
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, self.text)
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.phase = "confirm"
                self.next_at = time.monotonic() + 0.25
                return
            if self.phase == "confirm":
                _, value, _, _, _ = self.states[self.index]
                actual = self.parm.eval()
                assert actual == value if isinstance(value, str) else abs(actual - value) < 0.00001, (
                    self.parm.path(),
                    actual,
                    value,
                )
                self.phase = "wait"
            if not self.ready():
                return
            self.index += 1
            if self.index == len(self.states):
                self.finish()
            else:
                self.edit()
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
