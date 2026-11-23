# SPDX-License-Identifier: Apache-2.0
"""Isolate the GUI harness shutdown from renderer and native image connections."""

import json
import os
from pathlib import Path
import hou
from PySide6 import QtCore


def finish():
    Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(
        json.dumps(dict(result="passed", host_pid=os.getpid())), encoding="utf-8"
    )
    try:
        hou.exit(0, suppress_save_prompt=True)
    except SystemExit:
        pass


QtCore.QTimer.singleShot(2000, finish)
