# SPDX-License-Identifier: Apache-2.0
"""100,000 animated shared instances with camera/object edits during capture."""

import os
from pathlib import Path
import runpy

os.environ["CYCLES_HOUDINI_PACKED_PREVIEW_COUNT"] = "100000"
os.environ["CYCLES_HOUDINI_PACKED_PREVIEW_ROWS"] = "2"
os.environ["CYCLES_HOUDINI_PREVIEW_MATERIAL"] = "emission"
os.environ["CYCLES_HOUDINI_PREVIEW_LIGHT_TREE"] = "1"
runpy.run_path(
    str(Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/houdini_packed_preview_benchmark.py"),
    run_name="__main__",
)
