# SPDX-License-Identifier: Apache-2.0
"""Heavy adaptive courtyard navigation with denoising and actual preview captures."""

import os
from pathlib import Path
import runpy

os.environ["CYCLES_HOUDINI_LAYOUT_DENOISE"] = "1"
runpy.run_path(
    str(Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/houdini_camera_subdivision_ipr_smoke.py"),
    run_name="__main__",
)
