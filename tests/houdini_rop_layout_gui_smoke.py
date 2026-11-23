# SPDX-License-Identifier: Apache-2.0
"""Inspect the native parameter pane and resolve every Cycles shelf icon."""

import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import time
import traceback
import hou
from PySide6 import QtGui, QtWidgets


class LayoutWorkflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", screens=[], icons=[])
        self.rop = hou.node("/out").createNode("cycles_ipr", "cycles_layout_review")
        pass_fixture.parm(self.rop, "ipr_use_denoise").set(1)
        self.pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(1100, 1000))
        self.pane.setCurrentNode(self.rop)
        self.pane.setPin(True)
        screens = QtGui.QGuiApplication.screens()
        screen = next((screen for screen in screens if "UP2720Q" in screen.name()), None)
        if screen is None:
            candidates = [
                screen
                for screen in screens
                if (
                    round(screen.size().width() * screen.devicePixelRatio()),
                    round(screen.size().height() * screen.devicePixelRatio()),
                )
                == (3840, 2160)
            ]
            screen = candidates[0] if len(candidates) == 1 else None
        if screen is None:
            raise RuntimeError("The requested main DELL UP2720Q 3840x2160 display is unavailable")
        area = screen.availableGeometry()
        self.window = hou.qt.floatingPanelWindow(self.pane.floatingPanel())
        self.window.showNormal()
        if self.window.windowHandle():
            self.window.windowHandle().setScreen(screen)
        self.window.setGeometry(area.left() + 80, area.top() + 70, 1100, min(1000, area.height() - 140))
        self.screen = screen
        self.report["screen"] = dict(
            name=screen.name(),
            geometry=list(screen.geometry().getRect()),
            device_pixel_ratio=screen.devicePixelRatio(),
        )
        self.targets = [
            "noise_threshold",
            "denoise_start_sample",
            "ipr_denoise_start_sample",
            "max_bounce",
            "dof_mode",
            "dicing_rate",
            "vobject",
            "output_precision",
            "transparent_roughness_threshold",
            "tile_size",
            "auto_texture_cache",
            "pass_motion",
            "compute_backend",
        ]
        self.index = -1
        self.due = time.monotonic() + 1
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def icons(self):
        # Resolve the actual loaded shelf definitions, not merely XML strings.
        shelf = hou.shelves.shelves()["cycles_houdini"]
        self.contact = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(self.contact)
        for i, tool in enumerate(shelf.tools()):
            icon = hou.qt.Icon(tool.icon())
            pixmap = icon.pixmap(32, 32)
            assert not pixmap.isNull(), (tool.name(), tool.icon())
            label = QtWidgets.QLabel()
            label.setPixmap(pixmap)
            layout.addWidget(label, i, 0)
            layout.addWidget(QtWidgets.QLabel(tool.label()), i, 1)
            self.report["icons"].append(dict(tool=tool.name(), icon=tool.icon()))
        node_icons = [
            (hou.ropNodeTypeCategory(), "cycles_ipr", "cycles_render_rop"),
            (hou.objNodeTypeCategory(), "cycles::light::1", "cycles_area_light"),
            (hou.vopNodeTypeCategory(), "cycles::material::1", "cycles_material_preview"),
            (hou.vopNodeTypeCategory(), "cycles::volume_material::1", "cycles_material_preview"),
            (hou.vopNodeTypeCategory(), "cycles::world_material::1", "cycles_sky_world"),
            (hou.vopNodeTypeCategory(), "cycles::light_material::1", "cycles_light_material"),
        ]
        self.report["node_icons"] = []
        for category, name, expected in node_icons:
            node_type = hou.nodeType(category, name)
            assert node_type is not None, name
            assert node_type.icon() == expected, (name, node_type.icon(), expected)
            pixmap = hou.qt.Icon(node_type.icon()).pixmap(32, 32)
            assert not pixmap.isNull(), name
            row = layout.rowCount()
            label = QtWidgets.QLabel()
            label.setPixmap(pixmap)
            layout.addWidget(label, row, 0)
            layout.addWidget(QtWidgets.QLabel(node_type.description()), row, 1)
            self.report["node_icons"].append(dict(type=name, icon=expected))
        self.contact.adjustSize()
        self.contact.grab().save(str(self.path.with_suffix(".icons.png")))

    def tick(self):
        if time.monotonic() < self.due:
            return
        try:
            if self.index >= 0:
                filename = self.path.with_suffix("." + self.targets[self.index] + ".png")
                assert self.screen.geometry().contains(self.window.frameGeometry().center())
                self.window.grab().save(str(filename))
                self.report["screens"].append(str(filename))
            self.index += 1
            if self.index == len(self.targets):
                self.icons()
                self.report["result"] = "passed"
                self.path.write_text(json.dumps(self.report, indent=2))
                hou.ui.removeEventLoopCallback(self.callback)
                hou.exit(0, suppress_save_prompt=True)
                return
            parm = pass_fixture.parm(self.rop, self.targets[self.index])
            for folder, index in zip(parm.containingFolderSetParmTuples(), parm.containingFolderIndices()):
                template = self.rop.parmTemplateGroup().find(folder.name())
                folder[0].set(
                    1
                    if isinstance(template, hou.FolderParmTemplate)
                    and template.folderType() == hou.folderType.Collapsible
                    else index
                )
            if parm.name() == "ipr_denoise_start_sample":
                for p in self.rop.parms():
                    if p.parmTemplate().label() == "Final Render":
                        p.set(0)
                self.rop.updateParmStates()
                assert not parm.isDisabled()
            self.due = time.monotonic() + 0.6
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.path.write_text(json.dumps(self.report, indent=2))
            hou.ui.removeEventLoopCallback(self.callback)
            hou.exit(1, suppress_save_prompt=True)


workflow = LayoutWorkflow()
