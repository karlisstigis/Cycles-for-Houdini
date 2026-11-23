# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_VECTOR_VIEWER"] = "native"
from houdini_volume_vector_gui_smoke import Workflow

workflow = Workflow()
