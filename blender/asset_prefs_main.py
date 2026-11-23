# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Explicit Blender script: print the user's asset libraries after a host marker.

Run without --factory-startup so Blender loads the user's preferences:
blender --background --disable-autoexec --python asset_prefs_main.py
The host supplies a random marker in CH_ASSET_PREFS_MARKER.
"""

import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
# Blender receives a script path and deliberately inherits no host PYTHONPATH. This folder holds
# the Blender component (package cycles_blender); the shared Apache-2.0 modules it uses (pipe
# protocol, names, hashes, hull budgets) are in the Houdini package's python3.13libs, which sits
# beside it in a host root (<host>/python3.13libs) and under houdini/ in a source checkout.
_libs = _here.parent / "python3.13libs"
if not _libs.is_dir():
    _libs = _here.parent / "houdini" / "python3.13libs"
sys.path[:0] = [str(_here), str(_libs)]


def main():
    marker = os.environ.get("CH_ASSET_PREFS_MARKER", "")
    if len(marker) < 16 or "--help" in sys.argv:
        print("usage: blender --background --disable-autoexec --python asset_prefs_main.py")
        print("Requires CH_ASSET_PREFS_MARKER from the host.")
        return
    from cycles_blender.blend_asset_scan import print_asset_libraries

    print_asset_libraries(marker)


if __name__ == "__main__":
    main()
