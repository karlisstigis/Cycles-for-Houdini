# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

os.environ["CYCLES_HOUDINI_PACKED_FIELDS_VIEWER"] = "native"
exec(
    (
        Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/houdini_packed_instance_attribute_gui_smoke.py"
    ).read_text()
)
