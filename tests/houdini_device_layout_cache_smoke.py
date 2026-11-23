# SPDX-License-Identifier: Apache-2.0
"""Device layout reuse must not cache choices, expressions or stale HIP nodes."""

from support.paths import artifacts_root

import argparse
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
import hou
import cycles_houdini as ch
from cycles_houdini import device_selection as ds, native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device")
parser.parse_args()
rop = hou.node("/out").createNode("cycles_ipr", "layout_cache_test")
gpu = next(d for d in ch.devices() if d["backend"] == "OPTIX")
ds.set_devices(rop, gpu["id"])
expected = (gpu["id"],)
with patch.object(ds, "gpu_parameters", wraps=ds.gpu_parameters) as scan:
    ds._selection_layouts[rop.sessionId()] = None
    start = time.perf_counter()
    for _ in range(100):
        assert ds.selected_devices(rop, hou.time()) == expected
    cached = time.perf_counter() - start
    assert scan.call_count == 1, scan.call_count
    start = time.perf_counter()
    for _ in range(100):
        ds.gpu_parameters(rop)
    uncached = time.perf_counter() - start

parm = next(p for p in ds.gpu_parameters(rop) if p.tags()[ds._PHYSICAL] == gpu["physical_id"])
driver = hou.node("/obj").createNode("null", "device_expression_driver")
rop.parm(parm.name()).setExpression('ch("/obj/device_expression_driver/tx") > 0', hou.exprLanguage.Hscript)
driver.parm("tx").set(1)
assert ds.selected_devices(rop, hou.time()) == expected
driver.parm("tx").set(0)
try:
    ds.selected_devices(rop, hou.time())
    raise AssertionError("Stale checkbox expression")
except RuntimeError as error:
    assert "No GPU selected" in str(error)
driver.parm("tx").set(1)
assert ds.selected_devices(rop, hou.time()) == expected

group = rop.parmTemplateGroup()
replacement = group.find(parm.name())
tags = replacement.tags()
tags[ds._PHYSICAL] = "missing-test-device"
replacement.setTags(tags)
group.replace(parm.name(), replacement)
rop.setParmTemplateGroup(group)
try:
    ds.selected_devices(rop, hou.time())
    raise AssertionError("Stale GPU metadata")
except RuntimeError as error:
    assert "unavailable" in str(error)
tags[ds._PHYSICAL] = gpu["physical_id"]
replacement.setTags(tags)
group.replace(parm.name(), replacement)
rop.setParmTemplateGroup(group)
assert ds.selected_devices(rop, hou.time()) == expected
ds.refresh(rop)
assert ds.selected_devices(rop, hou.time()) == expected
identity = rop.sessionId()
rop.destroy()
assert identity not in ds._selection_layouts
rop = hou.node("/out").createNode("cycles_ipr", "layout_cache_test")
ds.set_devices(rop, gpu["id"])
assert ds.selected_devices(rop, hou.time()) == expected
identity = rop.sessionId()
hou.hipFile.clear(suppress_save_prompt=True)
assert identity not in ds._selection_layouts
report = dict(
    result="passed",
    cached_100_seconds=cached,
    layout_scan_100_seconds=uncached,
    checks=[
        "one scan per unchanged layout",
        "external checkbox expression",
        "layout metadata edit and repair",
        "refresh",
        "delete/recreate",
        "HIP clear",
    ],
)
destination = artifacts_root() / "viewer-optimization-20261027/device-cache.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
