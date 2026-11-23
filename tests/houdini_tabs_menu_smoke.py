# SPDX-License-Identifier: Apache-2.0
"""Owned GUI: registered shelf actions and native light/property interfaces."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
from PySide6 import QtCore

from cycles_houdini.scene import light_properties
from parameter_template_checks import native_layout


PRESETS = (
    ("point", "point", "point", None, False),
    ("spot", "spot", "point", None, True),
    ("sun", "sun", "sun", None, False),
    ("disk", "grid", "disk", "disk", False),
    ("area", "grid", "grid", "square", False),
)
PROPERTY_TABS = {
    "object": ("cycles_render_mesh", "Cycles", "vm_rendervisibility", "Render"),
    "light": ("cycles_light_radius", "Cycles Light", "light_type", "Light"),
    "camera": ("cycles_projection", "Cycles", "focal", "View"),
}
SHELF_TOOLS = [
    "cycles_add_object_parameters",
    "cycles_add_light_parameters",
    "cycles_add_camera_parameters",
    "cycles_remove_parameters",
    "cycles_create_area_light",
    "cycles_create_disk_light",
    "cycles_create_point_light",
    "cycles_create_spot_light",
    "cycles_create_sun_light",
    "cycles_create_environment",
    "cycles_create_sky_world",
    "cycles_create_light_material",
    "cycles_create_rop",
    "cycles_ipr",
    "cycles_material_preview",
    "cycles_import_blend",
]


def root_tabs(node):
    return list(node.parm("tx").containingFolderSetParmTuples()[0].parmTemplate().folderNames())


class TabWorkflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.stage = "object"
        self.began = time.monotonic()
        self.report = dict(result="running", events=[], host_pid=os.getpid(), render_viewers=[])
        obj = hou.node("/obj")
        self.targets = {
            "object": obj.createNode("geo", "native_object_controls"),
            "light": obj.createNode("hlight::2.0", "native_light_controls"),
            "camera": obj.createNode("cam", "native_camera_controls"),
        }
        self.targets["light"].setParms(dict(light_type="disk", coneenable=1, coneangle=36, conedelta=7, coneroll=2))
        self.targets["object"].parm("tx").setExpression("$F*.1", hou.exprLanguage.Hscript)
        self.targets["camera"].setParms(dict(focal=71, focus=9.25))
        self.original = {key: native_layout(node.parmTemplateGroup()) for key, node in self.targets.items()}
        self.original_tabs = {key: root_tabs(node) for key, node in self.targets.items()}
        # Verify the actual parameter editor and its native tabs through HOM.
        self.editor = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.Parm)
        assert self.editor is not None
        self.target = self.targets[self.stage]
        self.editor.setIsCurrentTab()
        self.editor.setCurrentNode(self.target)
        self.editor.setPin(True)
        self.window = hou.qt.mainWindow()
        self.window.show()
        self.busy = False
        self.invoked = False
        self.removing = False
        self.capture_index = None
        self.callback = self.tick
        hou.ui.addEventLoopCallback(self.callback)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2))

    def select_and_invoke(self, command):
        self.target.setSelected(True, clear_all_selected=True)
        self.target.setCurrent(True)
        assert hou.selectedNodes() == (self.target,), hou.selectedNodes()
        self.report.setdefault("actions", []).append({"tool": "cycles_" + command, "node": self.target.path()})
        self.save()
        viewer = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.SceneViewer)
        assert viewer is not None
        viewer.runShelfTool("cycles_" + command)

    def show_parameter(self, name, screenshot):
        self.editor.setCurrentNode(self.target)
        parm = self.target.parm(name)
        # Public HOM opens the native tab set. Its first parameter must be the
        # same for Transform and Cycles, so no outer renderer section exists.
        folder = parm.containingFolderSetParmTuples()[0]
        assert folder == self.target.parm("tx").containingFolderSetParmTuples()[0]
        self.capture_folder = folder[0]
        self.capture_tab = parm.containingFolderIndices()[0]
        self.capture_folder.set(self.capture_tab)
        # Native tab selection is editor state, not just the folder-set value.
        # Focus/scroll the actual control through the public ParameterEditor API.
        self.editor.scrollTo(parm, hou.scrollPosition.Top)
        self.editor.moveFocusTo(parm)
        assert self.editor.currentNode() == self.target
        assert self.capture_folder.eval() == self.capture_tab
        self.report.setdefault("selected_tabs", {})[screenshot] = parm.containingFolders()[0]
        self.screenshot = screenshot
        self.capture_at = time.monotonic() + 0.5
        self.capture_parm = parm.tuple()

    def check_shelf(self):
        shelf = hou.shelves.shelves()["cycles_houdini"]
        assert shelf.label() == "Cycles Tools"
        tools = [tool.name() for tool in shelf.tools()]
        # Development packages may omit the component flag. The panel registration identifies the component.
        configured = hou.getenv("CYCLES_HOUDINI_ASSET_BROWSER")
        addon_on_path = hou.pypanel.interfaceByName("cycles_houdini_assets") is not None
        if configured is not None:
            assert addon_on_path == (configured == "1"), configured
        expected = SHELF_TOOLS + (["cycles_asset_browser"] if addon_on_path else [])
        assert tools == expected, (tools, expected)
        assert all(tool.label() != "<undefined>" for tool in shelf.tools())
        self.report.update(shelf_tools=tools)

    def check_native_values(self):
        if self.stage == "object":
            assert self.target.parm("tx").expression() == "$F*.1"
        elif self.stage == "light":
            assert self.target.parm("light_type").evalAsString() == "disk"
            assert self.target.evalParm("coneenable") == 1
            assert [self.target.evalParm(name) for name in ("coneangle", "conedelta", "coneroll")] == [36, 7, 2]
        else:
            assert self.target.evalParm("focal") == 71 and self.target.evalParm("focus") == 9.25

    def check_properties(self):
        marker, expected, native, native_tab = PROPERTY_TABS[self.stage]
        if self.target.parm(marker) is None:
            return
        assert self.target.parmTemplateGroup().find("cycles_native_controls") is None
        labels = root_tabs(self.target)
        assert labels == self.original_tabs[self.stage] + [expected] and "Houdini" not in labels, labels
        assert self.target.parm("tx").containingFolders()[0] == "Transform"
        assert self.target.parm(native).containingFolders()[0] == native_tab
        self.check_native_values()
        self.report.setdefault("root_tabs", {})[self.stage] = labels
        self.screens = [
            ("tx", self.stage + "-transform"),
            (marker, self.stage + "-cycles"),
            (native, self.stage + "-native"),
        ]
        self.capture_index = 0
        self.show_parameter(*self.screens[0])

    def check_removed(self):
        marker = PROPERTY_TABS[self.stage][0]
        if self.target.parm(marker) is not None:
            return
        assert native_layout(self.target.parmTemplateGroup()) == self.original[self.stage]
        assert self.editor.currentNode() == self.target
        labels = root_tabs(self.target)
        assert labels == self.original_tabs[self.stage], labels
        self.check_native_values()
        self.report.setdefault("restored_tabs", {})[self.stage] = labels
        self.report["events"].append(f"Shelf Add and Remove Tabs preserve the native {self.stage} layout and values")
        self.removing = False
        if self.stage == "camera":
            self.check_shelf()
            self.preset_index = 0
            self.stage = "presets"
            self.run_preset()
            return
        self.stage = {"object": "light", "light": "camera"}[self.stage]
        self.target = self.targets[self.stage]
        self.editor.setCurrentNode(self.target)
        self.select_and_invoke("add_" + self.stage + "_parameters")

    def check_capture(self):
        if time.monotonic() < self.capture_at:
            return
        visible = self.editor.visibleParms()
        assert self.capture_parm in visible, (self.screenshot, [p.name() for p in visible])
        assert self.capture_folder.eval() == self.capture_tab
        self.report.setdefault("pane_controls", {})[self.screenshot] = self.capture_parm.name()
        # Capture after Houdini has painted the native tab on its event loop.
        assert self.window.grab().save(str(self.path.with_suffix("." + self.screenshot + ".png")))
        self.capture_index += 1
        if self.capture_index < len(self.screens):
            self.show_parameter(*self.screens[self.capture_index])
            return
        self.capture_index = None
        self.removing = True
        self.select_and_invoke("remove_parameters")

    def check_custom_removed(self):
        status, _ = hou.ui.statusMessage()
        if status != "Cycles: removed parameters from 0 node(s).":
            return
        assert native_layout(self.target.parmTemplateGroup()) == self.custom_layout
        assert root_tabs(self.target) == self.custom_root_tabs
        self.report["events"].append("Remove Tabs preserves the permanent Cycles Light interface")
        self.report.update(result="passed")
        self.finish(0)

    def prepare_custom(self, node):
        self.stage = "custom"
        self.target = node
        self.editor.setCurrentNode(node)
        group = node.parmTemplateGroup()
        visible = [
            entry.label()
            for entry in group.entries()
            if isinstance(entry, hou.FolderParmTemplate) and not entry.isHidden()
        ]
        assert visible == ["Transform", "Cycles Light"], visible
        # HDA instances canonicalize their native folder-set names. Labels and
        # contents, not the template author's root name, identify the tab.
        folder = next(
            entry
            for entry in group.entries()
            if isinstance(entry, hou.FolderParmTemplate) and entry.label() == "Cycles Light"
        )
        panels = [entry for entry in folder.parmTemplates() if isinstance(entry, hou.FolderParmTemplate)]
        assert [p.label() for p in panels] == [
            "Light",
            "Beam Shape",
            "Emission",
            "Linking",
            "Settings",
            "Ray Visibility",
            "Light Groups",
        ]
        assert all(p.folderType() == hou.folderType.Collapsible for p in panels)
        for name in (
            "cycles_light_type",
            "light_intensity",
            "cycles_light_radius",
            "cycles_light_spread",
            "cycles_light_cast_shadow",
            "cycles_lightgroup",
        ):
            assert node.parm(name).containingFolders()[0] == "Cycles Light"
        for kind in ("point", "spot", "grid", "sun"):
            node.parm("cycles_light_type").set(kind)
            node.updateParmStates()
            assert node.parm("vm_envangle").isHidden() == (kind != "sun")
            assert node.parm("areasizex").isHidden() == (kind != "grid")
            assert node.parm("cycles_light_radius").isHidden() == (kind not in ("point", "spot"))
            assert node.parm("cycles_light_spot_angle").isHidden() == (kind != "spot")
        node.parm("cycles_light_type").set("grid")
        self.custom_layout = native_layout(node.parmTemplateGroup())
        self.custom_root_tabs = root_tabs(node)
        self.report.update(custom_visible_tabs=visible, custom_panels=[p.label() for p in panels])
        self.report["events"].append(
            "Five light shelf presets use one custom type; four analytic types show relevant controls inside native collapsible panels"
        )
        self.screens = [("tx", "custom-transform"), ("cycles_light_type", "custom-area")]
        self.capture_index = 0
        self.show_parameter(*self.screens[0])

    def run_preset(self):
        preset = PRESETS[self.preset_index][0]
        self.preset_parent = hou.node("/obj")
        self.preset_prior_ids = {node.sessionId() for node in self.preset_parent.children()}
        self.preset_deadline = time.monotonic() + 5
        viewer = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.SceneViewer)
        viewer.setPwd(self.preset_parent)
        viewer.runShelfTool("cycles_create_" + preset + "_light")

    def check_preset(self):
        preset, kind, native, shape, cone = PRESETS[self.preset_index]
        created = tuple(node for node in self.preset_parent.children() if node.sessionId() not in self.preset_prior_ids)
        selected = hou.selectedNodes()
        assert len(created) <= 1, (preset, created)
        if not created or selected != created:
            assert time.monotonic() < self.preset_deadline, (preset, created, selected)
            return
        (node,) = created
        assert light_properties.is_cycles_light(node), (preset, node.type().name())
        assert node.type().definition().extraInfoValue("subtype") == "light"
        assert all(child.type().category() == hou.sopNodeTypeCategory() for child in node.children())
        assert node in self.preset_parent.recursiveGlob("*", filter=hou.nodeTypeFilter.ObjLight)
        authored = node.evalParm("cycles_light_type")
        native_type = node.parm("light_type").evalAsString()
        cone_enabled = bool(node.evalParm("coneenable"))
        assert (authored, native_type, cone_enabled) == (kind, native, cone), (
            preset,
            authored,
            native_type,
            cone_enabled,
        )
        if shape:
            assert node.evalParm("cycles_light_area_shape") == shape
            assert node.parmTuple("areasize").eval() == (1, 1)
        if cone:
            assert 0 < node.evalParm("coneangle") < node.evalParm("cycles_light_spot_angle")
        assert not node.errors(), node.errors()
        self.report.setdefault("presets", {})[preset] = dict(
            path=node.path(), authored_type=authored, native_type=native_type, shape=shape, cone=cone_enabled
        )
        self.save()
        self.preset_index += 1
        if self.preset_index < len(PRESETS):
            self.run_preset()
        else:
            self.prepare_custom(node)

    def finish(self, code):
        self.save()
        hou.ui.removeEventLoopCallback(self.callback)
        hou.ui.postEventCallback(lambda: hou.exit(code, suppress_save_prompt=True))

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            if time.monotonic() - self.began > 30:
                raise TimeoutError(self.stage)
            if not self.invoked:
                self.invoked = True
                self.select_and_invoke("add_object_parameters")
            elif self.stage == "presets":
                self.check_preset()
            elif self.capture_index is not None:
                self.check_capture()
            elif self.stage == "custom":
                self.check_custom_removed()
            elif self.removing:
                self.check_removed()
            else:
                self.check_properties()
        except Exception:
            self.report.update(result="failed", stage=self.stage, error=traceback.format_exc())
            self.finish(1)
        finally:
            self.busy = False


def start():
    global workflow
    try:
        workflow = TabWorkflow()
        workflow.save()
    except Exception:
        path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        path.write_text(json.dumps(dict(result="failed", stage="setup", error=traceback.format_exc()), indent=2))
        hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))


QtCore.QTimer.singleShot(1500, start)
