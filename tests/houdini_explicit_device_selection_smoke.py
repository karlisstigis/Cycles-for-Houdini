# SPDX-License-Identifier: Apache-2.0
"""Exact scripting selections override saved checkboxes without a render."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import selected_devices as rop_devices
import argparse
import json
import os
from pathlib import Path

import hou
from cycles_houdini import devices, device_selection, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/devices/explicit"
destination.mkdir(parents=True, exist_ok=True)
rop = hou.node("/out").createNode("cycles_ipr", "explicit_devices")
checks = []
available = devices()
for backend in ("CUDA", "OPTIX"):
    ids = tuple(sorted(device["id"] for device in available if device["backend"] == backend))
    assert ids, backend
    assert device_selection.set_devices(rop, ids) == ids
    # Reducing a checked GPU group to an already-selected card is exact.
    same = rop_devices(rop, hou.time())[0]
    assert same in ids
    assert device_selection.set_devices(rop, same) == (same,)
    assert rop_devices(rop, hou.time())[0] == same
    for value in ids:
        assert device_selection.set_devices(rop, value) == (value,)
    assert device_selection.set_devices(rop, ids) == ids
    checks.append(dict(backend=backend, devices=ids, unchanged_id_selects_one=True))
assert device_selection.set_devices(rop, "CPU") == ("CPU",)
before = device_selection.selected_devices(rop, hou.time())
gpu = next(value["id"] for value in available if value["backend"] == "OPTIX")
for invalid in ((), ("CPU", "CPU"), ("CPU", gpu), ("unavailable_device",)):
    try:
        device_selection.set_devices(rop, invalid)
        raise AssertionError("Invalid explicit selection was accepted")
    except ValueError:
        pass
    assert device_selection.selected_devices(rop, hou.time()) == before
assert not registry.entries(), "Device controls allocated a renderer"
report = dict(result="passed", checks=checks, invalid_selections=4, renderer_allocated=False)
(destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", backend_groups=len(checks), invalid_selections=4)))
