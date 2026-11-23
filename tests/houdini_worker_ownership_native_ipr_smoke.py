# SPDX-License-Identifier: Apache-2.0
"""One native IPR worker and persistent host pipe through native settings edits."""

import os
from pathlib import Path
import runpy

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
OwnershipWorkflow = runpy.run_path(
    str(root / "tests/houdini_worker_ownership_ipr_smoke.py"), run_name="native_ownership_fixture"
)["OwnershipWorkflow"]
workflow = OwnershipWorkflow(native=True)
