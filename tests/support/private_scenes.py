# SPDX-License-Identifier: Apache-2.0
"""Scenes that exist only on the owner's workstation, found through environment variables.

A few benchmarks and reproductions open real artist scenes that are not part of the
repository. They never name a machine path: CYCLES_HOUDINI_ARTIST_SCENES names the folder
that holds them (the folder that contains Cycles_Test/), and a scene used by one test
alone can have its own variable. When the scene is not available the test stops with
the report result "skipped" and a message that says what it needs; that is not a pass.
"""

import json
import os
from pathlib import Path

ARTIST_SCENES = "CYCLES_HOUDINI_ARTIST_SCENES"


def skip(message):
    """Record a skipped GUI/hython run and stop it."""
    print(message, flush=True)
    report = os.environ.get("CYCLES_HOUDINI_GUI_REPORT")
    if report:
        Path(report).write_text(json.dumps(dict(result="skipped", error=message), indent=2), encoding="utf-8")
    try:
        import hou
    except ImportError:
        raise SystemExit(message) from None
    hou.exit(3, suppress_save_prompt=True)
    raise SystemExit(message)


def artist_scene(relative):
    """Path of a private scene below $CYCLES_HOUDINI_ARTIST_SCENES, or a recorded skip."""
    root = os.environ.get(ARTIST_SCENES, "")
    if root and (Path(root) / relative).is_file():
        return Path(root) / relative
    if not root:
        skip(f"Skipped: needs the private artist scene {relative}. Set {ARTIST_SCENES} to the folder that holds it.")
    skip(f"Skipped: {relative} was not found below {ARTIST_SCENES}={root}.")


def scene_file(variable, description):
    """Path of a private scene named by its own environment variable, or a recorded skip."""
    value = os.environ.get(variable, "")
    if value and Path(value).is_file():
        return Path(value)
    skip(f"Skipped: needs {description}. Set {variable} to the scene file." + (f" Not found: {value}" if value else ""))
