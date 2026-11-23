# SPDX-License-Identifier: Apache-2.0
"""Houdini startup script for the asset browser trial (scripts/test-asset-browser.ps1).

Registers the sample library made by tests/blend_asset_scan_smoke.py (once)
and docks the asset browser through its own shelf tool, so the shelf tool is
exercised too. Never writes into a library folder.
"""

import os

import hou


def register_sample():
    sample = os.environ.get("CYCLES_HOUDINI_ASSET_SAMPLE_LIBRARY", "").strip()
    if not sample or not os.path.isdir(sample):
        return
    from cycles_houdini.assets import libraries

    current, error = libraries.read_registry()
    if error:
        print("Asset browser trial: library registry unreadable, sample not added: " + error)
        return
    key = os.path.normcase(os.path.abspath(sample))
    if any(os.path.normcase(os.path.abspath(library.path)) == key for library in current):
        return
    current.append(libraries.Library(name="Cycles sample assets", path=sample))
    libraries.save_libraries(current)


def open_browser():
    try:
        tool = hou.shelves.tool("cycles_asset_browser")
    except Exception:
        tool = None
    if tool is None:
        hou.ui.displayMessage(
            "The Cycles Asset Browser add-on is not on the Houdini path in this session "
            "(started without the asset browser component).",
            title="Asset browser trial",
        )
        return
    exec(compile(tool.script(), "cycles_asset_browser shelf tool", "exec"), {"hou": hou, "kwargs": {}})


register_sample()
open_browser()
