# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_COORDINATE_VIEWER"] = "native"
from houdini_shutter_coordinate_gui_smoke import Workflow

workflow = Workflow()
