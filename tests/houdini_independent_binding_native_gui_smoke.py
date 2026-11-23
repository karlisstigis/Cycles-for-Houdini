# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_BINDING_VIEWER"] = "native"
from houdini_independent_binding_gui_smoke import Workflow

workflow = Workflow()
