# SPDX-License-Identifier: Apache-2.0
"""Exercise large cached triangle ranges against independent uncached images."""

import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_LIGHT_TREE_ROWS"] = "129"
os.environ["CYCLES_HOUDINI_LIGHT_TREE_COLS"] = "129"
os.environ["CYCLES_HOUDINI_REQUIRE_PARALLEL_LIGHT_TREE"] = "1"
os.environ["CYCLES_HOUDINI_LIGHT_TREE_REFERENCE_HISTORY"] = "1"
exec(Path(__file__).with_name("houdini_light_tree_cache_smoke.py").read_text())
