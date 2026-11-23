# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_CD_VIEWER"] = "native"
exec((Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/houdini_packed_cd_gui_smoke.py").read_text())
