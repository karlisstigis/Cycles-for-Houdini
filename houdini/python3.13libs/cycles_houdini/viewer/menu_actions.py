# SPDX-License-Identifier: Apache-2.0
"""Public menu actions; standard objects never acquire renderer properties silently."""

import hou
from ..scene import object_properties, light_properties, camera_properties


def _selected_objects(nodes):
    result = []
    for node in nodes:
        while node and node.type().category() != hou.objNodeTypeCategory():
            node = node.parent()
        if node and node not in result:
            result.append(node)
    return result


def add_properties(kind="objects", nodes=None):
    nodes = _selected_objects(hou.selectedNodes() if nodes is None else nodes)
    install = {
        "objects": object_properties.install,
        "lights": light_properties.install,
        "cameras": camera_properties.install,
    }[kind]
    changed = []
    skipped = []
    with hou.undos.group("Add Cycles " + kind + " Parameters"):
        for node in nodes:
            try:
                if kind == "objects" and light_properties.supported(node):
                    skipped.append(node.path())
                    continue
                if install(node):
                    changed.append(node.path())
            except (hou.PermissionError, ValueError) as error:
                skipped.append(node.path() + ": " + str(error))
    if hou.isUIAvailable():
        hou.ui.setStatusMessage(
            "Cycles: added parameters to "
            + str(len(changed))
            + " node(s)."
            + (" Skipped: " + ", ".join(skipped) if skipped else "")
        )
    return dict(changed=changed, skipped=skipped)


def remove_properties(nodes=None):
    changed = []
    with hou.undos.group("Remove Cycles Parameters"):
        for node in _selected_objects(hou.selectedNodes() if nodes is None else nodes):
            # Cycles Light authors these as asset parameters; remove only spare tabs.
            if light_properties.is_cycles_light(node):
                continue
            group = node.parmTemplateGroup()
            camera_folder = next(
                (
                    f
                    for f in group.entries()
                    if isinstance(f, hou.FolderParmTemplate) and "cycles_camera_properties_version" in f.tags()
                ),
                None,
            )
            if camera_folder:
                group.remove(camera_folder)
                node.setParmTemplateGroup(group)
                changed.append(node.path())
                continue
            folder = object_properties.properties_folder(group) or object_properties.properties_folder(group, "lights")
            owned = hou.ParmTemplateGroup([folder]) if folder else None
            if owned and (owned.find("cycles_subdivision_override") or owned.find("cycles_light_visibility_override")):
                node.setParmTemplateGroup(object_properties.remove_tabs(group))
                changed.append(node.path())
    if hou.isUIAvailable():
        hou.ui.setStatusMessage("Cycles: removed parameters from " + str(len(changed)) + " node(s).")
    return changed


def create_light(kind="grid"):
    allowed = ("point", "spot", "grid", "area", "square", "rectangle", "disk", "ellipse", "sun", "distant")
    if kind not in allowed:
        raise ValueError("Unknown Cycles light preset: " + str(kind))
    area = kind in ("grid", "area", "square", "rectangle", "disk", "ellipse")
    obj = hou.node("/obj")
    node = obj.createNode(
        light_properties.LIGHT_TYPE, "cycles_" + ("area" if kind in ("grid", "area", "rectangle") else kind) + "_light"
    )
    node.parm("cycles_light_type").set("grid" if area else "sun" if kind == "distant" else kind)
    if area:
        node.parm("cycles_light_area_shape").set("square" if kind in ("grid", "area") else kind)
    if kind == "distant":
        node.parm("vm_envangle").set(0)
    node.moveToGoodPosition()
    node.setCurrent(True, clear_all_selected=True)
    if kind in ("sun", "distant"):
        node.parm("light_intensity").set(1)
    return node


def create_rop():
    node = hou.node("/out").createNode("cycles_ipr", "cycles_render")
    node.setParms(dict(source_mode="objects", vobject="*", lights="*"))
    node.moveToGoodPosition()
    node.setCurrent(True, clear_all_selected=True)
    return node


def create_environment():
    """A native environment light keeps its icon, rotation state and host tabs."""
    with hou.undos.group("Create Cycles environment light"):
        node = hou.node("/obj").createNode("envlight", "cycles_environment")
        light_properties.install(node)
        node.parm("light_contribprimary").set(1)
        node.moveToGoodPosition()
        node.setCurrent(True, clear_all_selected=True)
    return node


def create_light_material():
    from ..materials import create_light_material as create

    node = create()
    node.moveToGoodPosition()
    node.setCurrent(True, clear_all_selected=True)
    if hou.isUIAvailable():
        hou.ui.setStatusMessage(
            "Cycles: assign this material under your light Emission / Emission Material. Light Color and Power multiply it."
        )
    return node


def create_sky_world():
    """An editable native World Material; the artist chooses its ROP binding."""
    from ..materials import create_world_material

    node = create_world_material(name="cycles_sky_world", sky=True)
    node.moveToGoodPosition()
    node.setCurrent(True, clear_all_selected=True)
    if hou.isUIAvailable():
        hou.ui.setStatusMessage("Cycles: choose this World Material under Scene / Environment on the render ROP.")
    return node


RENDER_PANEL = "cycles_houdini_render"


def cycles_rops():
    """Every Cycles Render ROP in the scene, ordered by path."""
    node_type = hou.nodeType(hou.ropNodeTypeCategory(), "cycles_ipr")
    return sorted(node_type.instances(), key=lambda node: node.path()) if node_type else []


def target_rop(create=True):
    """The ROP a shelf or menu action renders: a selected Cycles ROP, else the scene's first one.

    With `create`, a scene without one gets /out/cycles_render (one undo step).
    """
    for node in hou.selectedNodes():
        if node.type().name() == "cycles_ipr":
            return node
    rops = cycles_rops()
    if rops:
        return rops[0]
    if not create:
        return None
    with hou.undos.group("Create Cycles render ROP"):
        return create_rop()


def start_ipr():
    """Shelf IPR: open the floating Cycles Render View for the target ROP and start rendering."""
    from ..rop import render_actions

    return render_actions.open_ipr(target_rop())


def _render_tabs():
    for tab in hou.ui.paneTabs():
        if tab.type() == hou.paneTabType.PythonPanel:
            interface = tab.activeInterface()
            if interface is not None and interface.name() == RENDER_PANEL:
                yield tab


def open_render_view():
    """Main menu Render View: show the docked Cycles Render panel (reuse one), bound to the target ROP.

    Docks beside the Scene View of the current desktop; floats only when there is no pane.
    Opening the view does not start a render or create a ROP.
    """
    interface = hou.pypanel.interfaceByName(RENDER_PANEL)
    if interface is None:
        raise hou.Error("The Cycles Render panel is not installed. Run Cycles Setup again.")
    tab = next(_render_tabs(), None)
    if tab is None:
        anchor = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer) or hou.ui.paneTabOfType(
            hou.paneTabType.NetworkEditor
        )
        if anchor is not None and anchor.pane() is not None:
            tab = anchor.pane().createTab(hou.paneTabType.PythonPanel)
        else:
            tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        tab.setActiveInterface(interface)
    tab.setIsCurrentTab()
    rop = target_rop(create=False)
    if rop is not None:
        try:
            panel = tab.activeInterfaceRootWidget()
            if panel is not None and hasattr(panel, "set_rop"):
                panel.set_rop(rop)
        except hou.Error:
            pass
    return tab
