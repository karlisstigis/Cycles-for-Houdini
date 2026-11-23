# SPDX-License-Identifier: Apache-2.0
"""Run the same subdivision toggle image checks through Houdini native IPR."""

import os
from pathlib import Path
import runpy

os.environ["CYCLES_HOUDINI_SUBDIV_VIEWER"] = "native"
runpy.run_path(str(Path(__file__).with_name("houdini_subdivision_ipr_toggle_gui_smoke.py")), run_name="__main__")
