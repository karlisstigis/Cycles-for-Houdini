# SPDX-License-Identifier: Apache-2.0
"""Assert the runtime selected by the launcher inside the real Houdini process."""

import json
import os
from pathlib import Path


def verify(test=None):
    if "CYCLES_HOUDINI_TEST_IDENTITY" not in os.environ:
        return
    import hou
    import cycles_houdini as ch
    from cycles_houdini import _native

    expected = json.loads(os.environ["CYCLES_HOUDINI_TEST_IDENTITY"])
    host = Path(expected["host"]).resolve()
    assert hou.applicationVersionString() == expected["houdini"]
    assert Path(ch.__file__).resolve().is_relative_to(host), ch.__file__
    assert Path(_native.__file__).resolve().is_relative_to(host), _native.__file__
    assert Path(hou.getenv("CYCLES_HOUDINI_RESOURCES")).resolve() == host / "resources"
    assert hou.nodeType(hou.ropNodeTypeCategory(), "cycles_ipr") is not None
    definition = hou.nodeType(hou.ropNodeTypeCategory(), "cycles_ipr").definition()
    assert Path(definition.libraryFilePath()).resolve().is_relative_to(host), definition.libraryFilePath()
    if expected["release"]:
        assert hou.getenv("CYCLES_HOUDINI_RELEASE_ID") == expected["release"]
    report = dict(
        result="passed",
        run_id=os.environ.get("CYCLES_HOUDINI_TEST_RUN_ID"),
        houdini=hou.applicationVersionString(),
        python=ch.__file__,
        native=_native.__file__,
        hda=definition.libraryFilePath(),
        resources=hou.getenv("CYCLES_HOUDINI_RESOURCES"),
        release=hou.getenv("CYCLES_HOUDINI_RELEASE_ID"),
        manifest_sha256=expected["manifest_sha256"],
    )
    if test:
        report["test"] = Path(test).name
    Path(os.environ["CYCLES_HOUDINI_TEST_IDENTITY_REPORT"]).write_text(json.dumps(report, indent=2))
