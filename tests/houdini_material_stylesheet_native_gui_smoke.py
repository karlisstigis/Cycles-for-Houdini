# SPDX-License-Identifier: Apache-2.0
import os, runpy
from pathlib import Path

os.environ["CYCLES_HOUDINI_STYLES_VIEWER"] = "native"
runpy.run_path(str(Path(__file__).with_name("houdini_material_stylesheet_gui_smoke.py")), run_name="__main__")
