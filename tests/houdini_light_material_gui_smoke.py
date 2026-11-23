# SPDX-License-Identifier: Apache-2.0
"""Prepare native light controls; observe real mouse clicks on + and Open."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore
from cycles_houdini import light_materials, registry
from light_shader_fixture import Fixture


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.f = Fixture("CPU")
        self.f.rop.parm("ipr_samples").set(1000000)
        self.report = dict(
            result="running",
            stage="mount",
            mouse_input="Computer Use; callbacks are observed, not invoked by the fixture",
        )
        self.finished = False
        self.busy = False
        self.began = time.monotonic()
        self.opened = 0
        self.settled = 0
        desktop = hou.ui.curDesktop()
        self.network = desktop.createFloatingPaneTab(hou.paneTabType.NetworkEditor, size=(850, 550))
        self.network.setPwd(hou.node("/mat"))
        self.network.qtParentWindow().setWindowTitle("Cycles Light Material - network test")
        self.settings = desktop.createFloatingPaneTab(hou.paneTabType.Parm, size=(900, 650))
        self.settings.setCurrentNode(self.f.light)
        self.settings.setPin(True)
        self.settings.qtParentWindow().setWindowTitle("Cycles Light Material - controls test")
        parm = self.f.light.parm("cycles_light_material")
        for folder, index in zip(parm.containingFolderSetParmTuples(), parm.containingFolderIndices()):
            folder[0].set(1 if folder.parmTemplate().folderType() == hou.folderType.Collapsible else index)
        self.settings.scrollTo(parm, hou.scrollPosition.Center)
        self.tab = desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(850, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.original_focus = light_materials.focus

        def observed_focus(light, editor=None):
            status = self.panel.entry.session.status()
            self.report["before_open_revision"] = status["render_revision"]
            self.report["before_open_sample"] = status.get("current_sample", 0)
            material = self.original_focus(light, editor)
            self.opened += 1
            self.opened_at = time.monotonic()
            self.report["focused_material"] = material.path()
            self.report["network_pwd"] = self.network.pwd().path()
            self.report["network_current"] = self.network.currentNode().path()
            self.report["material_count_after_open"] = len(hou.node("/mat").children())
            self.save()
            return material

        light_materials.focus = observed_focus
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(50)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str))

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            if self.finished:
                if self.path.with_suffix(".finish").exists() or time.monotonic() - self.began > 230:
                    self.request_quit()
                return
            assert time.monotonic() - self.began < 210, ("GUI click timeout", self.report)
            if self.panel is None:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.f.rop)
                self.panel.queue_render()
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            path = self.f.light.evalParm("cycles_light_material")
            if self.report["stage"] == "mount":
                if status.get("current_sample", 0) <= 0 or status.get("pending_update"):
                    return
                self.report.update(
                    stage="await_plus", initial_generation=status["generation"], renderer_pid=status["renderer_pid"]
                )
                self.settings.setIsCurrentTab()
                parm = self.f.light.parm("cycles_light_material_create")
                for folder, index in zip(parm.containingFolderSetParmTuples(), parm.containingFolderIndices()):
                    folder[0].set(1 if folder.parmTemplate().folderType() == hou.folderType.Collapsible else index)
                self.settings.scrollTo(self.f.light.parm("cycles_light_material"), hou.scrollPosition.Center)
                self.settings.moveFocusTo("cycles_light_material_create")
                self.save()
            elif self.report["stage"] == "await_plus" and path:
                if status.get("pending_update") or status.get("capture_pending"):
                    return
                if status["generation"] <= self.report["initial_generation"]:
                    return
                self.created = hou.node(path)
                assert self.created.type().name() == "cycles::light_material::1"
                assert self.created.name() == self.f.light.name()
                self.f.light.updateParmStates()
                assert self.f.light.parm("cycles_light_material_create").isHidden()
                assert not self.f.light.parm("cycles_light_material_open").isHidden()
                self.report.update(
                    stage="await_open",
                    created_material=path,
                    material_count_before_open=len(hou.node("/mat").children()),
                    created_generation=status["generation"],
                )
                self.settings.moveFocusTo("cycles_light_material_open")
                self.save()
            elif self.report["stage"] == "await_open":
                if not self.opened:
                    return
                assert self.report["network_pwd"] == "/mat"
                assert self.report["network_current"] == path
                assert self.report["material_count_after_open"] == self.report["material_count_before_open"]
                if time.monotonic() - self.opened_at < 1.5:
                    return
                # Scene submissions may be reused after selection changes. Only
                # render_revision denotes a restarted image accumulation.
                assert status["render_revision"] == self.report["before_open_revision"], status
                assert status.get("current_sample", 0) >= self.report["before_open_sample"], status
                assert status["renderer_pid"] == self.report["renderer_pid"]
                self.report.update(
                    result="passed",
                    stage="review",
                    open_calls=self.opened,
                    final_generation=status["generation"],
                    final_render_revision=status["render_revision"],
                    final_sample=status.get("current_sample", 0),
                )
                self.finished = True
                self.save()
                entry.session.cancel()
        except BaseException:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.request_quit()
        finally:
            self.busy = False

    def request_quit(self):
        self.timer.stop()
        hou.ui.postEventCallback(self.quit)

    def quit(self):
        self.timer.stop()
        light_materials.focus = self.original_focus
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.save()
        self.settings.close()
        self.network.close()
        self.tab.close()
        self.timer.deleteLater()
        hou.ui.postEventCallback(
            lambda: hou.exit(0 if self.report["result"] == "passed" else 1, suppress_save_prompt=True)
        )


workflow = Workflow()
