# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_SAMPLED_INSTANCE_VIEWER"] = "native"
from houdini_sampled_instance_attribute_gui_smoke import Workflow

workflow = Workflow()
