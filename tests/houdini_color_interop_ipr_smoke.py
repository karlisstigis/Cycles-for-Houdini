# SPDX-License-Identifier: Apache-2.0
"""Portable Auto headers through real native IPR and shared-panel color epochs."""

import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_TEST_TEXTURE_PORTABLE"] = "1"
source = Path(__file__).with_name("houdini_color_ipr_smoke.py")
exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), globals())
