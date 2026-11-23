# SPDX-License-Identifier: Apache-2.0
"""Atomic test evidence publication despite short Windows reader/scanner locks."""

import json
from pathlib import Path
import time
import uuid


def atomic_json(path, value):
    """Publish complete JSON, retrying only replace errors compatible with a short lock.

    Windows readers that omit FILE_SHARE_DELETE can briefly deny replacement.
    Ten attempts wait at most 1.375 seconds; a persistent denial still fails the
    test. The prior report remains intact until the atomic replacement succeeds.
    """
    path = Path(path)
    encoded = json.dumps(value, indent=2, allow_nan=False)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".writing")
    owned = False
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            owned = True
            stream.write(encoded)
        for attempt in range(10):
            try:
                temporary.replace(path)
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 9:
                    raise
                time.sleep(min(0.025 * 2**attempt, 0.2))
    finally:
        if owned:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A scanner can also lock this private file. Cleanup must not mask
                # the original publication error, which is still propagated.
                pass
