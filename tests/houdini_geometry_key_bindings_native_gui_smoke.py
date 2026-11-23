# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_KEY_BINDINGS_VIEWER"] = "native"
from houdini_geometry_key_bindings_gui_smoke import start

start()
