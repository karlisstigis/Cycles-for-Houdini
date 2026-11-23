# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_RAMP_OVERRIDE_VIEWER"] = "native"
from houdini_material_ramp_override_gui_smoke import start

start()
