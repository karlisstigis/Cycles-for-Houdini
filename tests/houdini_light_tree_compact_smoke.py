# SPDX-License-Identifier: Apache-2.0
"""Exact compact mesh-light blocks against independent uncached image histories."""

import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_FORCE_COMPACT_LIGHT_TREE_CACHE"] = "1"
os.environ["CYCLES_HOUDINI_LIGHT_TREE_ROWS"] = "129"
os.environ["CYCLES_HOUDINI_LIGHT_TREE_COLS"] = "129"
os.environ["CYCLES_HOUDINI_REQUIRE_PARALLEL_LIGHT_TREE"] = "1"
os.environ["CYCLES_HOUDINI_LIGHT_TREE_REFERENCE_HISTORY"] = "1"
exec((Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/houdini_light_tree_cache_smoke.py").read_text())
