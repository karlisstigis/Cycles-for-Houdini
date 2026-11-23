# SPDX-License-Identifier: Apache-2.0
"""Blender asset library browser.

The core modules (catalogs, libraries, records, cache, providers, indexer) are
pure Python and import neither `hou` nor Qt. Importing this package loads none
of its submodules.
"""

import os


def enabled():
    """CYCLES_HOUDINI_ASSET_BROWSER; unset means enabled (development checkouts)."""
    value = os.environ.get("CYCLES_HOUDINI_ASSET_BROWSER")
    if value is None:
        return True
    return value.strip().lower() not in ("0", "off", "false", "disabled")
