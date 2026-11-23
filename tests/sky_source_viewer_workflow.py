# SPDX-License-Identifier: Apache-2.0
"""One viewer at a time: numeric sky faults, independent edits and live repair."""

import json, os, time, traceback
from pathlib import Path
import hou, numpy as np
from PySide6 import QtCore
from cycles_houdini import registry, native_scene
from sky_source_recovery_fixture import Fixture, CASES

_workflow = None


def start(kind):
    global _workflow
    _workflow = Workflow(kind)


class Workflow:
    def __init__(self, kind):
        self.kind = kind
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture(os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop = self.f.rop
        self.report = dict(
            result="running", viewer=kind, events=[], scope=__doc__ + " Scripted HOM edits, not a latency certificate."
        )
        self.panel = self.session = self.retiring = None
        self.index = self.phase = self.generation = 0
        self.baseline = None
        self.stage = "mount"
        self.busy = self.finished = False
        self.started = time.monotonic()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(
            hou.paneTabType.PythonPanel if kind == "cycles" else hou.paneTabType.IPRViewer, size=(900, 650)
        )
        if kind == "cycles":
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        else:
            self.tab.setRopNode(self.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
            self.tab.startRender()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(25)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def save(self):
        self.report.update(phase=self.stage, case=CASES[self.index] if self.index < len(CASES) else "")
        self.path.write_text(json.dumps(self.report, indent=2))

    def edit(self, change):
        self.stage = "editing"

        def perform():
            try:
                change()
                self.stage = "wait"
            except BaseException:
                self.finish(traceback.format_exc())

        hou.ui.postEventCallback(perform)

    def ready(self):
        entry = registry.get(f"rop:{self.rop.sessionId()}")
        if entry is None:
            return False
        self.session = entry.session
        status = self.session.status()
        assert not entry.error and status["state"] != "failed", (entry.error, status)
        frame = self.session.frame_info()
        unused = self.f.case == "unused" and self.phase == 1 and entry.change_serial == entry.applied_serial
        if (
            status.get("capture_pending")
            or status.get("pending_update")
            or not frame
            or not frame["complete"]
            or not frame.get("current_scene")
        ):
            return False
        if not unused and (frame["generation"] <= self.generation or frame["generation"] != status["generation"]):
            return False
        if self.kind == "cycles":
            if (
                self.panel.canvas.image.isNull()
                or self.panel.canvas.frame_metadata.get("sequence") != frame["sequence"]
            ):
                return False
        elif not entry.helper_pid or self.tab.imageResolution() != (128, 64):
            return False
        image = self.session.frame()
        if self.kind == "native":
            actual = self.tab.pixel("C", 96, 32)[:3]
            expected = self.f.plane(image, "combined")[32, 96, :3]
            if not np.allclose(actual, expected, atol=5e-4, rtol=0):
                return False
        broken = self.phase in (1, 2)
        values = self.f.check(
            image, status, broken, self.baseline if self.phase == 3 else None, 0.1 if self.phase == 2 else 0, atol=5e-4
        )
        if self.phase == 0:
            self.baseline = values
            self.pid = status["renderer_pid"]
        else:
            assert status["renderer_pid"] == self.pid, status
        self.generation = frame["generation"]
        self.report["events"].append(dict(case=self.f.case, step=self.phase, status=status))
        return True

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 320, self.stage
            if self.stage == "mount":
                if self.kind == "cycles":
                    self.panel = self.tab.activeInterfaceRootWidget()
                    if self.panel is None:
                        return
                    self.panel.set_rop(self.rop)
                    self.panel.auto.setChecked(True)
                    self.panel.render_button.click()
                self.stage = "wait"
                return
            if self.stage == "closing":
                status = self.retiring.status()
                if status["state"] != "closed":
                    return
                assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
                self.report["closed"] = status
                self.finish()
                return
            if self.stage != "wait" or not self.ready():
                return
            self.phase += 1
            if self.phase == 1:
                self.edit(lambda: self.f.set_broken(True))
            elif self.phase == 2:

                def move():
                    self.f.objects[0].parm("tz").set(0.1)
                    self.f.camera.parm("tx").set(0.05)

                self.edit(move)
            elif self.phase == 3:

                def repair():
                    self.f.objects[0].parm("tz").set(0)
                    self.f.camera.parm("tx").set(0)
                    self.f.set_broken(False)

                self.edit(repair)
            else:
                self.index += 1
                if self.index == len(CASES):
                    self.retiring = self.session
                    if self.panel:
                        self.panel.dispose()
                    else:
                        self.tab.killRender()
                    registry.close_all()
                    self.stage = "closing"
                else:
                    self.phase = 0
                    self.baseline = None
                    self.edit(lambda: self.f.configure(CASES[self.index]))
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False
            if not self.finished:
                self.save()

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        if self.panel:
            self.panel.dispose()
        elif self.kind == "native":
            self.tab.killRender()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error, seconds=time.monotonic() - self.started)
        self.save()
        hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))
