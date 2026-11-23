# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_CONSTANT_FIELDS_VIEWER"] = "native"
from houdini_constant_attribute_gui_smoke import Workflow

workflow = Workflow()
