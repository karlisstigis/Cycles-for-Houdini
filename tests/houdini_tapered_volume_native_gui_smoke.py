# SPDX-License-Identifier: Apache-2.0
"""The same nonlinear edit/repair workflow in Houdini's native IPR viewer."""

import os

os.environ["CYCLES_HOUDINI_TAPERED_VIEWER"] = "native"
from houdini_tapered_volume_gui_smoke import start

if __name__ == "__main__":
    start()
