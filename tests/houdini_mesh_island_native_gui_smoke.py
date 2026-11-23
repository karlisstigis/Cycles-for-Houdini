# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_ISLAND_VIEWER"] = "native"
exec(compile(Path(__file__).with_name("houdini_mesh_island_gui_smoke.py").read_text(), __file__, "exec"))
