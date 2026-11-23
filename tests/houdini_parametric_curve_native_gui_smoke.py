# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_CURVE_VIEWER"] = "native"
from houdini_parametric_curve_gui_smoke import Workflow

workflow = Workflow()
