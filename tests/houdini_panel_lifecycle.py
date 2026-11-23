# SPDX-License-Identifier: Apache-2.0
"""Isolate Python Panel creation/destruction without rendering or image transport."""

import json
import os
from pathlib import Path
import hou
from PySide6 import QtCore


report = dict(host_pid=os.getpid(), result="running")
path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))


def finish():
    assert tab.activeInterfaceRootWidget() is not None, tab.activeInterfaceScriptErrors()
    tab.close()
    report["result"] = "passed"
    path.write_text(json.dumps(report), encoding="utf-8")
    QtCore.QTimer.singleShot(500, exit_test)


def exit_test():
    try:
        hou.exit(0, suppress_save_prompt=True)
    except SystemExit:
        pass


QtCore.QTimer.singleShot(1500, finish)
