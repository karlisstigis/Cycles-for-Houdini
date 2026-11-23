# SPDX-License-Identifier: Apache-2.0
"""Interrupted retained uploads with exact compact mesh-light records."""

import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_FORCE_COMPACT_LIGHT_TREE_CACHE"] = "1"
exec(Path(__file__).with_name("houdini_light_tree_upload_smoke.py").read_text())
