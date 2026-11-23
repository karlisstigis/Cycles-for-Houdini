# SPDX-License-Identifier: Apache-2.0
"""One-shot conversion of saved scenes to the integer-vector Cycles Resolution.

The Cycles ROP replaced its separate Width and Height parameters with one
Resolution vector (res). The plugin carries no migration code: run this once on
scenes saved before the change, in hython with the current Cycles package:

  scripts/run-hython.ps1 -HythonArguments @('scripts/convert-rop-resolution.py','<scene.hiplc>')

For every Cycles ROP the old values are read from the saved scene text, the scene
is loaded and Resolution set, and the scene saved in place. The original is kept
next to it as <name>.pre-res<ext>. Animated or expression-driven Width/Height are
reported and left for manual conversion. A JSON report is printed.
"""

import json
from pathlib import Path
import re
import shutil
import sys

import hou

HEADER = rb"HouLC\x1a?[0-9a-f]{28}"


def old_resolutions(data):
    """Map ROP path -> (width, height) from a saved .hiplc/.hipnc text."""
    result = {}
    for match in re.finditer(HEADER + rb"([^\x00]+)\.init\x00type = cycles_ipr\b", data):
        path = match.group(1).decode("utf-8")
        start = data.find(path.encode("utf-8") + b".parm\x00")
        if start < 0:
            continue
        end = re.search(HEADER, data[start:])
        section = data[start : start + end.start()] if end else data[start:]
        values = {}
        for name in ("width", "height"):
            found = re.search(rb"\n" + name.encode() + rb"\s+\[[^\]]*\]\s+\(\s*([^)]*?)\s*\)", section)
            if found:
                text = found.group(1).decode("utf-8", "replace").strip()
                values[name] = int(text) if re.fullmatch(r"-?\d+", text) else text
        result["/" + path] = values
    return result


def convert(scene):
    scene = Path(scene).resolve()
    if not scene.is_file():
        return dict(scene=str(scene), result="missing", reason="No such scene file")
    data = scene.read_bytes()
    if not re.search(HEADER, data):
        return dict(scene=str(scene), result="skipped", reason="Only .hiplc/.hipnc text scenes are supported")
    found = old_resolutions(data)
    if not found or not any(found.values()):
        return dict(scene=str(scene), result="unchanged", reason="No Cycles ROP with saved Width/Height")
    hou.hipFile.load(scene.as_posix(), suppress_save_prompt=True, ignore_load_warnings=True)
    converted, manual = [], []
    for path, values in found.items():
        node = hou.node(path)
        if node is None or node.parmTuple("res") is None:
            manual.append(dict(rop=path, reason="ROP missing or not the current definition"))
            continue
        current = list(node.parmTuple("res").eval())
        for index, name in enumerate(("width", "height")):
            value = values.get(name)
            if isinstance(value, int):
                current[index] = value
            elif value is not None:
                manual.append(
                    dict(rop=path, parm=name, saved=value, reason="animated or expression; set Resolution by hand")
                )
        node.parmTuple("res").set(current)
        converted.append(dict(rop=path, res=current))
    backup = scene.with_name(scene.stem + ".pre-res" + scene.suffix)
    if not backup.exists():
        shutil.copy2(scene, backup)
    hou.hipFile.save(scene.as_posix())
    return dict(scene=str(scene), result="converted", backup=str(backup), rops=converted, manual=manual)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    report = [convert(path) for path in sys.argv[1:]]
    print(json.dumps(report, indent=1))
    hou.hipFile.clear(suppress_save_prompt=True)
