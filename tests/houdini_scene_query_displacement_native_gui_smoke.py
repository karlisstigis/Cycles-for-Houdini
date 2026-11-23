# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_QUERY_VIEWER"] = "native"
from scene_query_displacement_viewer_workflow import start

start()
