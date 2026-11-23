# SPDX-License-Identifier: Apache-2.0
"""Check the exercised binary against the current public transport contract."""

import os
import re
from pathlib import Path


def expected_worker_protocol():
    path = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "include/cycles_houdini/snapshot.h"
    match = re.search(r"\bworker_protocol_version\s*=\s*(\d+)\s*;", path.read_text())
    assert match, "The public worker protocol declaration could not be read"
    return int(match.group(1))
