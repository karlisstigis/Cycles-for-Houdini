# SPDX-License-Identifier: Apache-2.0
"""Retired: Color Ramp stops are edited inline in the parameter pane.

Only kept because materials promoted before the inline ramps saved an "Edit
Color Ramp" button whose callback imports this module. Pressing it converts
that hidden RGBA promotion to inline Color and Alpha ramps; it opens no window.
"""


def edit(target):
    import hou
    from .ramp_promotion import convert_legacy

    template = target.parmTemplate() if isinstance(target, hou.Parm) else None
    if template is None or template.tags().get("cycles_rgba_ramp") != "1":
        raise RuntimeError("Cycles Color Ramp stops are edited inline in the parameter pane")
    node, name = target.node(), target.name()

    def convert():
        convert_legacy(node, name)
        if hou.isUIAvailable():
            hou.ui.setStatusMessage("Converted " + node.path() + "/" + name + " to inline Color and Alpha ramps")

    if hou.isUIAvailable():
        # The pressed button belongs to the replaced interface: convert after
        # its callback returns.
        import hdefereval

        hdefereval.executeDeferred(convert)
    else:
        convert()
