# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_VELOCITY_NORMAL_VIEWER"] = "native"
from houdini_velocity_normal_gui_smoke import Workflow

workflow = Workflow()
