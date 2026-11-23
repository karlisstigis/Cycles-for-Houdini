# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import re
import runpy
import sys
import traceback
import json
import time
import hou


def select_viewer_display():
    """Run any GUI test with a chosen viewer display: -RunName <name>__display_<gpu|native|classic>.

    The viewer reads CYCLES_HOUDINI_VIEWER_DISPLAY when a panel is created;
    without the suffix the product default applies.
    """
    match = re.search(r"__display_(gpu|native|classic)", Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent.name)
    if match:
        os.environ["CYCLES_HOUDINI_VIEWER_DISPLAY"] = match.group(1)


def start_test():
    try:
        select_viewer_display()
        sys.path.insert(0, str(Path(os.environ["CYCLES_HOUDINI_GUI_TEST"]).parent))
        from support.paths import configure_test_environment

        configure_test_environment()
        from native_runtime_identity import verify

        verify()
        runpy.run_path(os.environ["CYCLES_HOUDINI_GUI_TEST"], run_name="__main__")
    except Exception:
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(
            json.dumps(dict(result="failed", error=traceback.format_exc())), encoding="utf-8"
        )
        try:
            hou.exit(1, suppress_save_prompt=True)
        except SystemExit:
            pass


ready_at = time.monotonic() + 0.5


def wait_for_host():
    if time.monotonic() >= ready_at:
        hou.ui.removeEventLoopCallback(wait_for_host)
        hou.ui.postEventCallback(start_test)


hou.ui.addEventLoopCallback(wait_for_host)
