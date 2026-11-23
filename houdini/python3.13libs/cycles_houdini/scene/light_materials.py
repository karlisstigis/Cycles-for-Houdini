# SPDX-License-Identifier: Apache-2.0
"""Explicit light-material creation and navigation from the light controls."""

import hou


def focus(light, editor=None):
    from .light_shaders import material_path

    path = material_path(light, hou.time())
    material = hou.node(path) if path else None
    if material is None:
        raise ValueError("The assigned light material does not exist: " + (path or "<empty>"))
    if hou.isUIAvailable():
        if editor is None:
            editors = [pane for pane in hou.ui.paneTabs() if isinstance(pane, hou.NetworkEditor)]
            editor = next((pane for pane in editors if pane.isCurrentTab() and pane.pwd() == material.parent()), None)
            if editor is None:
                editor = next((pane for pane in editors if pane.isCurrentTab()), editors[0] if editors else None)
            if editor is None:
                editor = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.NetworkEditor)
        editor.setIsCurrentTab()
        editor.setPwd(material.parent())
        material.setCurrent(True, clear_all_selected=True)
        editor.setCurrentNode(material)
        editor.homeToSelection()
        window = editor.qtParentWindow()
        if window is not None:
            window.raise_()
            window.activateWindow()
    return material


def create_or_focus(light, editor=None):
    """An empty assignment creates once; an existing assignment only navigates."""
    from .light_properties import supported
    from .light_shaders import material_path
    from ..materials import create_light_material

    if not supported(light) or light.parm("cycles_light_material") is None:
        raise ValueError("Choose a Cycles light or a native light with Cycles parameters")
    if material_path(light, hou.time()):
        return focus(light, editor)
    with hou.undos.group("Create and assign Cycles light material"):
        material = create_light_material(name=light.name())
        try:
            light.parm("cycles_light_material").set(material.path())
        except BaseException:
            material.destroy()
            raise
        material.moveToGoodPosition()
    if hou.isUIAvailable():
        hou.ui.setStatusMessage("Created " + material.path() + ". Use Open to focus the light material.")
    return material


def action(kwargs):
    try:
        return create_or_focus(kwargs["node"])
    except (ValueError, hou.Error) as error:
        if not hou.isUIAvailable():
            raise
        hou.ui.setStatusMessage(str(error), severity=hou.severityType.Warning)
        return None
