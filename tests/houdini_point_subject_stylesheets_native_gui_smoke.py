# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_KEY_BINDINGS_VIEWER"] = "native"
from houdini_point_subject_stylesheets_gui_smoke import start

start()
