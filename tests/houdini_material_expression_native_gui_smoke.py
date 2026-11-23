# SPDX-License-Identifier: Apache-2.0
"""Native Houdini IPR display of material expression changes and repairs."""

import os

os.environ["CYCLES_HOUDINI_EXPRESSION_VIEWER"] = "native"
from houdini_material_expression_gui_smoke import start

if __name__ == "__main__":
    start()
