# SPDX-License-Identifier: Apache-2.0
"""Navigation, source repair and cancellation during dense motion-binding preparation."""

import os
from houdini_stylesheet_cooperative_gui_smoke import Workflow

os.environ["CYCLES_HOUDINI_COOPERATIVE_MOTION"] = "1"
workflow = Workflow()
