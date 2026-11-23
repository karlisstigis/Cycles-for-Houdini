# SPDX-License-Identifier: Apache-2.0
import os
from houdini_stylesheet_cooperative_gui_smoke import Workflow

os.environ["CYCLES_HOUDINI_COOPERATIVE_VIEWER"] = "native"
workflow = Workflow()
