# SPDX-License-Identifier: Apache-2.0
import os

os.environ["CYCLES_HOUDINI_STYLES_VIEWER"] = "native"
from houdini_stylesheet_sources_gui_smoke import Workflow

workflow = Workflow()
