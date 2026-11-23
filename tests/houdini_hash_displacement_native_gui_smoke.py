# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_TRANSPORT_VIEWER"] = "native"
from hash_displacement_viewer_workflow import start

start()
