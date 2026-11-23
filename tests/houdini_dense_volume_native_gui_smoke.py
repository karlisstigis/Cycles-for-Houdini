# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_DENSE_VIEWER"] = "native"
from houdini_dense_volume_gui_smoke import Workflow

workflow = Workflow()
