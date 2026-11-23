# SPDX-License-Identifier: Apache-2.0
"""One viewer: packed subdivision off/on, uniform/adaptive, and retained worker."""

from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import device_selection, object_properties, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", events=[], physical_mouse_certification=False)
        self.obj = hou.node("/obj").createNode("geo", "ipr_subdivision_packed", run_init_scripts=False)
        box = self.obj.createNode("box")
        self.pack = self.obj.createNode("pack")
        self.pack.setInput(0, box)
        self.pack.setDisplayFlag(True)
        self.pack.setRenderFlag(True)
        object_properties.install(self.obj)
        pass_fixture.set_parameters(self.obj, dict(cycles_subdivision_override=1, cycles_subdivision_ipr_level=2))
        self.camera = hou.node("/obj").createNode("cam", "ipr_subdivision_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=2))
        self.rop = hou.node("/out").createNode("cycles_ipr", "ipr_subdivision_toggle")
        configure_render(
            self.rop,
            dict(
                max_subdivisions=4,
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=96,
                resy=96,
                samples=8,
                ipr_samples=8,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                use_material_assignments=0,
                material_kind="emission",
                emission_strength=1,
                pass_depth=1,
            ),
        )
        self.rop.parmTuple("basecolor").set((1, 1, 1))
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.actions = [
            ("Uniform subdivision enabled by default", {}, True),
            ("Disable uniform subdivision live", dict(cycles_subdivision_ipr=0), False),
            ("Re-enable uniform subdivision live", dict(cycles_subdivision_ipr=1), True),
            (
                "Enable adaptive object-space subdivision",
                dict(
                    cycles_subdivision_adaptive=1, cycles_subdivision_space="object", cycles_subdivision_edge_length=0.1
                ),
                True,
            ),
            ("Disable adaptive subdivision live", dict(cycles_subdivision_ipr=0), False),
            ("Re-enable adaptive subdivision live", dict(cycles_subdivision_ipr=1), True),
        ]
        self.native = os.environ.get("CYCLES_HOUDINI_SUBDIV_VIEWER") == "native"
        self.report["viewer"] = "native" if self.native else "cycles"
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 750))
        self.settings.setCurrentNode(self.obj)
        for parameters in self.obj.parmTuples():
            template = parameters.parmTemplate()
            if isinstance(template, hou.FolderSetParmTemplate) and "Cycles" in template.folderNames():
                parameters[0].set(template.folderNames().index("Cycles"))
                break
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(
            hou.paneTabType.IPRViewer if self.native else hou.paneTabType.PythonPanel
        )
        self.panel = None
        self.index = 0
        self.generation = 0
        self.pid = None
        self.finished = self.busy = False
        self.began = time.monotonic()
        self.phase = "mount"
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(20)
        if self.native:
            self.tab.setRopNode(self.rop)
            self.tab.setPreview(True)
            self.tab.setAutoUpdate(True)
            self.tab.setDelay(0.02)
            self.tab.startRender()
            self.phase = "wait"
        else:
            self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.began < 100, (self.phase, self.index)
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "wait"
                return
            entry = registry.get(f"rop:{self.rop.sessionId()}")
            if entry is None:
                return
            status = entry.session.status()
            info = entry.session.frame_info()
            assert status["state"] != "failed" and not entry.error, (status, entry.error)
            if not info or not info["complete"] or info["generation"] <= self.generation:
                return
            if status.get("pending_update") or status.get("capture_pending"):
                return
            if entry.change_serial > entry.applied_serial:
                return
            if self.panel and self.panel._live_generation != info["generation"]:
                return
            label, _, subdivided = self.actions[self.index]
            frame = entry.session.frame()
            plane = frame["planes"]["depth"]
            depth = float(np.frombuffer(plane["pixels"], np.float32).reshape(96, 96, plane["channels"])[48, 48, 0])
            assert (status["subdivision_control_faces"] == 6) == subdivided, (label, status)
            assert 4.57 < depth < 4.65 if subdivided else abs(depth - 4.5) < 1e-5, (label, depth)
            assert self.pack.geometry().intrinsicValue("primitivecount") == 1
            assert status["geometry_resources"] == 1 and len(registry.entries()) == 1, status
            if self.pid is None:
                self.pid = status["renderer_pid"]
            assert status["renderer_pid"] == self.pid
            self.report["events"].append(dict(label=label, depth=depth, status=status))
            self.generation = info["generation"]
            self.index += 1
            if self.index == len(self.actions):
                hou.qt.floatingPanelWindow(self.settings.floatingPanel()).grab().save(
                    str(self.path.with_suffix(".parameters.png"))
                )
                self.finish()
                return
            with hou.undos.group(self.actions[self.index][0]):
                pass_fixture.set_parameters(self.obj, self.actions[self.index][1])
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        if self.panel:
            self.panel.dispose()
        if self.native:
            self.tab.killRender()
        registry.close_all()
        self.settings.close()
        self.tab.close()
        self.timer.deleteLater()
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
