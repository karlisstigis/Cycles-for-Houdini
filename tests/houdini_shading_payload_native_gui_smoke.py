# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_SHADING_VIEWER"] = "native"
from houdini_shading_payload_gui_smoke import Workflow

workflow = Workflow()
