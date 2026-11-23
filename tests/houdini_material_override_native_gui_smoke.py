# SPDX-License-Identifier: Apache-2.0
"""Run the same native material override workflow in Houdini's IPR viewer."""

import os

os.environ["CYCLES_HOUDINI_OVERRIDE_VIEWER"] = "native"
from houdini_material_override_gui_smoke import Workflow

workflow = Workflow()
