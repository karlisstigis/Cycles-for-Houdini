# SPDX-License-Identifier: Apache-2.0
"""One live viewer: Blender hue modes, native controls, recovery and heavy sharing."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import registry
from hue_ramp_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.native = os.environ.get("CYCLES_HOUDINI_HUE_VIEWER") == "native"
        self.f = f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        f.height = 96
        f.rop.parm("resy").set(96)
        self.heavy = f.add_heavy()
        self.key = f"rop:{f.rop.sessionId()}"
        self.steps = []

        def choose(name):
            return lambda: f.choose(name, hou.rampBasis.BSpline)

        for mode in ("hsl", "hsv"):
            for direction in ("near", "far", "cw", "ccw"):
                self.steps.append((mode + " " + direction, choose(mode + "_" + direction + "_red_blue"), False))
        for name in ("duplicate_first", "duplicate_last", "duplicate_interior", "maximum_keys", "hdr"):
            self.steps.append(("HSL " + name, choose("hsl_near_" + name), False))
        self.steps.append(("Return to two stops", choose("hsl_far_red_blue"), False))
        for index in range(2):
            self.steps.append(
                ("Broken HSL " + str(index), lambda: f.ramp.parmTuple("ramp1c").set((1.5, 0.5, 0.7)), True)
            )
            self.steps.append(("Repair " + str(index), choose("hsl_far_red_blue"), False))
        self.steps.append(
            ("Invalid authored hue menu", lambda: f.ramp.parm("ramp_hue_interpolation").set("not_a_hue"), True)
        )
        self.steps.append(("Repair authored hue menu", choose("hsl_far_red_blue"), False))
        self.steps += [
            ("Camera navigation", lambda: f.camera.parm("ty").set(0.1), False),
            ("Packed object movement", lambda: self.heavy.parm("tx").set(0.5), False),
            ("Native basis stays dormant", lambda: f.ramp.parm("ramp1interp").set("constant"), False),
            ("Rename ramp", lambda: f.ramp.setName("renamed_color_ramp"), False),
        ]
        self.steps[0][1]()
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 600))
        self.settings.setPin(True)
        self.settings.setCurrentNode(f.ramp)
        self.panel = None
        if self.native:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
            self.tab.setRopNode(f.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
        else:
            self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.report = dict(result="running", viewer="native" if self.native else "cycles", scope=__doc__, events=[])
        self.index = self.generation = 0
        self.phase = "mount"
        self.began = time.monotonic()
        self.busy = self.finished = False
        self.entry = self.session = None
        hou.ui.addEventLoopCallback(self.tick)
        if self.native:
            self.tab.startRender()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            if self.phase == "closing":
                status = self.session.status() if self.session else None
                if status and status["state"] != "closed" and time.monotonic() < self.deadline:
                    return
                if status:
                    assert (
                        status["state"] == "closed"
                        and status["device_allocation_bytes"] == status["owned_geometry_bytes"] == 0
                    ), status
                self.report.update(result="failed" if self.error else "passed", error=self.error, closed=status)
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1 if self.error else 0, suppress_save_prompt=True))
                return
            assert time.monotonic() - self.began < 240, (self.phase, self.index)
            if self.phase == "mount":
                if not self.native:
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.f.rop)
                    QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
            entry = registry.get(self.key)
            if entry is None:
                return
            self.entry = entry
            status = entry.session.status()
            info = entry.session.frame_info()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if entry.change_serial != entry.applied_serial or entry.requested_serial:
                return
            dormant_basis = self.steps[self.index][0] == "Native basis stays dormant"
            if (
                not info
                or not info["complete"]
                or (info["generation"] <= self.generation and not dormant_basis)
                or info["generation"] != status["generation"]
                or status.get("capture_pending")
                or status.get("pending_update")
            ):
                return
            if not self.native and (
                self.panel._live_generation != info["generation"]
                or self.panel.canvas.frame_metadata.get("sequence") != info["sequence"]
            ):
                return
            label, _, warning = self.steps[self.index]
            frame = entry.session.frame()
            error = self.f.inspect(frame, (1, 0, 1) if warning else None)
            assert bool(status["source_diagnostics"]) == warning, status
            self.f.ramp.updateParmStates()
            assert (
                not self.f.ramp.parm("ramp_hue_interpolation").isHidden()
                and self.f.ramp.parm("ramp1interp").isDisabled()
            )
            assert self.f.ramp.parm("factor").isDisabled()
            if self.native:
                if not entry.helper_pid:
                    return
                i = len(self.f.factors) // 2
                x = 4 * i + 2
                y = self.f.height // 2
                expected = (1, 0, 1) if warning else self.f.case["colors"][i]
                try:
                    if not np.allclose(self.tab.pixel("C", x, y)[:3], expected, atol=3e-5, rtol=3e-5):
                        return
                except hou.OperationFailed:
                    return
            else:
                assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
            assert len(registry.entries()) == 1 and status["packed_instances"] == 12, status
            if self.report["events"]:
                first = self.report["events"][0]
                assert (
                    status["renderer_pid"] == first["status"]["renderer_pid"]
                    and status["geometry_conversions"] == first["status"]["geometry_conversions"]
                    and self.f.heavy.cookCount() == first["heavy_cooks"]
                ), status
            self.generation = info["generation"]
            self.report["events"].append(
                dict(label=label, max_error=error, status=status, heavy_cooks=self.f.heavy.cookCount())
            )
            self.save()
            self.index += 1
            if self.index == len(self.steps):
                if self.panel:
                    self.panel.grab().save(str(self.path.with_suffix(".png")))
                hou.qt.floatingPanelWindow(self.settings.floatingPanel()).grab().save(
                    str(self.path.with_name(self.path.stem + "-parameters.png"))
                )
                self.close()
                return
            self.steps[self.index][1]()
        except BaseException:
            if self.phase == "closing":
                self.report.update(result="failed", error=traceback.format_exc())
                self.finished = True
                self.save()
                hou.ui.removeEventLoopCallback(self.tick)
                hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
            else:
                self.close(traceback.format_exc())
        finally:
            self.busy = False

    def close(self, error=None):
        self.error = error
        self.session = self.entry.session if self.entry else None
        if self.native:
            self.tab.killRender()
        elif self.panel:
            QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
            self.panel.dispose()
        registry.close_all()
        self.phase = "closing"
        self.deadline = time.monotonic() + 20


if __name__ == "__main__":
    workflow = Workflow()
