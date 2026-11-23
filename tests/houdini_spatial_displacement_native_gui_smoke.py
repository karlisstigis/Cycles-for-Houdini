# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_SPATIAL_VIEWER"] = "native"
from houdini_spatial_displacement_gui_smoke import Workflow

workflow = Workflow()
