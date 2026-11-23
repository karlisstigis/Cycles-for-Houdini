# SPDX-License-Identifier: Apache-2.0
"""Exercise the dense material-motion profile with authored per-primitive bindings."""

import os
import runpy
from pathlib import Path

os.environ["CYCLES_HOUDINI_MATERIAL_PROFILE_BINDINGS"] = "1"
runpy.run_path(str(Path(__file__).with_name("houdini_material_motion_acquisition_profile.py")), run_name="__main__")
