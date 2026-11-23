# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_CAMERA_LENS_VIEWER"] = "native"
from houdini_camera_lens_motion_gui_smoke import Workflow

workflow = Workflow()
