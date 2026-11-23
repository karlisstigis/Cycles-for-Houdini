# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_OBJECT_SHUTTER_VIEWER"] = "native"
from houdini_object_binding_shutter_gui_smoke import Workflow

workflow = Workflow()
