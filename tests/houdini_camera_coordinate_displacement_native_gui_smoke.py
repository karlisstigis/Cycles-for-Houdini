# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_COORDINATE_VIEWER"] = "native"
from camera_coordinate_displacement_viewer_workflow import start

start()
