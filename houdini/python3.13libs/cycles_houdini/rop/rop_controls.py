# SPDX-License-Identifier: Apache-2.0
"""Native parameter-menu actions. No scene cooking or rendering."""

import hou

_controls_menu = None


def show_controls(rop):
    """Keep secondary job actions out of the main ROP parameter layout."""
    from PySide6 import QtGui, QtWidgets
    from . import background_render

    global _controls_menu
    if _controls_menu is not None:
        _controls_menu.close()
        _controls_menu.deleteLater()
    menu = _controls_menu = QtWidgets.QMenu(hou.qt.mainWindow())
    menu.setObjectName("cycles_rop_controls")

    def action(name, label, icon, callback):
        item = menu.addAction(menu.style().standardIcon(icon), label)
        item.setData(name)
        item.triggered.connect(lambda checked=False: callback())
        return item

    icons = QtWidgets.QStyle.StandardPixmap
    action(
        "render_settings",
        "Render Settings…",
        icons.SP_FileDialogDetailedView,
        lambda: rop.parm("renderdialog").pressButton(),
    )
    menu.addSeparator()
    action(
        "render_background", "Render to Disk in Background", icons.SP_MediaPlay, lambda: background_render.launch(rop)
    )
    action("background_jobs", "Background Renders…", icons.SP_FileDialogListView, background_render.show_jobs)
    action("cancel_background", "Cancel Background Render", icons.SP_DialogCancelButton, lambda: cancel_render(rop))
    menu.popup(QtGui.QCursor.pos())
    return menu


def cancel_render(rop):
    """Stop this ROP's foreground render, or its last submitted background job."""
    from . import final_render, background_render

    if final_render.request_cancel(rop):
        message = "Cancelling Cycles render — " + rop.path()
    else:
        job = background_render.last_job(rop)
        if job is None or job.status()["state"] in background_render._terminal:
            message = "No active render for this ROP. Use Background Renders to inspect other jobs."
        else:
            job.stop()
            message = "Cancelling Cycles background render — " + rop.path()
    if hou.isUIAvailable():
        hou.ui.setStatusMessage(message)


def value(rop, name, now):
    """Read the current ROP schema; missing configuration is an explicit error."""
    parm = rop.parm(name)
    if parm is None:
        raise ValueError(
            f"Cycles ROP {rop.path()} is missing required parameter {name}; recreate it from the current definition"
        )
    return parm.evalAtTime(now)


def output_color_menu():
    from .. import color_policy

    return [part for name in color_policy()["output_spaces"] for part in (name, name)]


def apply_resolution_menu(kwargs):
    """Apply a Houdini FBres menu token: "width height [pixel aspect]"."""
    fields = str(kwargs.get("script_value", "")).split()
    if len(fields) < 2 or not all(field.isdigit() for field in fields[:2]):
        return
    values = dict(resx=int(fields[0]), resy=int(fields[1]))
    if len(fields) > 2:
        values["pixel_aspect"] = float(fields[2])
    with hou.undos.group("Cycles Resolution"):
        kwargs["node"].setParms(values)


def material_menu(world=False):
    from ..materials import BUILDER, VOLUME_BUILDER, WORLD_BUILDER

    kinds = (WORLD_BUILDER,) if world else (BUILDER, VOLUME_BUILDER)
    paths = sorted(
        node.path()
        for node in hou.node("/").allSubChildren(recurse_in_locked_nodes=False)
        if node.type().name() in kinds
    )
    return [part for path in paths for part in (path, path)]
