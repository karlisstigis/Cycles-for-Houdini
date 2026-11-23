# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_RAMP_VIEWER"] = "native"
from houdini_ramp_interpolation_gui_smoke import Workflow

workflow = Workflow()
