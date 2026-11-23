# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_OVERRIDE_VIEWER"] = "native"
from houdini_sampled_override_gui_smoke import Workflow

workflow = Workflow()
