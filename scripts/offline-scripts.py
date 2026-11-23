# SPDX-License-Identifier: Apache-2.0
"""Generate self-contained saved-HIP guards: available even without the package."""

import textwrap

NOTICE = """import hou
if not getattr(hou.session, '_cycles_missing_notice', False):
    hou.session._cycles_missing_notice = True
    print('Cycles renderer unavailable: saved values and graph recovery data are retained.')
if not hasattr(hou.session, '_cycles_missing_notice_reset'):
    def _cycles_missing_notice_reset(event):
        if event in (hou.hipFileEventType.BeforeLoad, hou.hipFileEventType.AfterClear):
            hou.session._cycles_missing_notice = False
    hou.session._cycles_missing_notice_reset = _cycles_missing_notice_reset
    hou.hipFile.addEventCallback(_cycles_missing_notice_reset)
"""


def guard(script, *, menu=False):
    """Only missing top-level integration modules are expected offline failures."""
    names = "('cycles_houdini',)" if menu else "('cycles_houdini', 'cycles_houdini_startup')"
    result = (
        "try:\n"
        + textwrap.indent(script.rstrip() + "\n", "    ")
        + "except ModuleNotFoundError as _cycles_import_error:\n"
        + f"    if _cycles_import_error.name not in {names}:\n        raise\n"
        + textwrap.indent(NOTICE, "    ")
    )
    if menu:
        # rawValue does not evaluate the dynamic menu being generated. Do not
        # write a parameter or normalize its saved token while displaying a menu.
        result += (
            "    _cycles_parm = kwargs.get('parm')\n"
            "    _cycles_saved = _cycles_parm.rawValue() if _cycles_parm is not None else ''\n"
            "    return [_cycles_saved, _cycles_saved or '(Unavailable)'] if _cycles_saved else []\n"
        )
    return result


def guard_menus(group):
    def visit(parm):
        if hasattr(parm, "parmTemplates"):
            parm.setParmTemplates(tuple(visit(p) for p in parm.parmTemplates()))
        elif hasattr(parm, "itemGeneratorScript"):
            code = parm.itemGeneratorScript()
            # Houdini also stores native one-line expression menus here (for
            # example the resolution chooser). They have different return
            # semantics and need no integration import guard.
            if code and "from cycles_houdini" in code:
                parm.setItemGeneratorScript(guard(code, menu=True))
        return parm

    for parm in group.entries():
        group.replace(parm.name(), visit(parm))
    return group
