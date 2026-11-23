# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_POINTINESS_VIEWER"] = "native"
from houdini_pointiness_gui_smoke import Workflow

workflow = Workflow()
