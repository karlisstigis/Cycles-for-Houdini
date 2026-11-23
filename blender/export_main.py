# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Explicit Blender script entrypoint; implementation imports keep package identity."""

from pathlib import Path
import sys

_here = Path(__file__).resolve().parent
# Blender receives a script path and deliberately inherits no host PYTHONPATH. This folder holds
# the Blender component (package cycles_blender); the shared Apache-2.0 modules it uses (pipe
# protocol, names, hashes, hull budgets) are in the Houdini package's python3.13libs, which sits
# beside it in a host root (<host>/python3.13libs) and under houdini/ in a source checkout.
_libs = _here.parent / "python3.13libs"
if not _libs.is_dir():
    _libs = _here.parent / "houdini" / "python3.13libs"
sys.path[:0] = [str(_here), str(_libs)]
from cycles_blender.blend_export import main


if __name__ == "__main__":
    main()
