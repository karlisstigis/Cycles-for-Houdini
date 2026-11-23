# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_PANORAMA_VIEWER"] = "native"
from houdini_panorama_gui_smoke import Workflow

workflow = Workflow()
