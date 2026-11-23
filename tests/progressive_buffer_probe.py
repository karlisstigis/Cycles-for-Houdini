# SPDX-License-Identifier: Apache-2.0
"""Measure active buffer growth without any viewer; allocation control probe."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
hou.hipFile.load(
    str(artifacts_root() / "stage3/materials/ramps/native-material-ramps.hiplc"), suppress_save_prompt=True
)
hou.setFrame(1)
rop = hou.node("/out/native_ramp_cycles")
reports = []
for device in ("CPU", ch.default_device()):
    set_rop_devices(rop, device)
    with ch.Session(device=device, width=64, height=64, samples=65536, collect_diagnostics=True) as session:
        generation = session.apply(native_scene.capture(rop))
        deadline = time.monotonic() + 5
        while not session.frame_info():
            assert time.monotonic() < deadline, session.status()
            time.sleep(0.001)
        baseline = session.status()
        observations = []
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            status = session.status()
            assert status["generation"] == generation
            assert status["geometry_resources"] == baseline["geometry_resources"]
            assert status["material_resources"] == baseline["material_resources"]
            if not observations or status["device_allocation_bytes"] != observations[-1]["device_allocation_bytes"]:
                observations.append(status)
            time.sleep(0.001)
        session.cancel()
        joined = session.status()
        time.sleep(0.05)
        assert session.status()["device_allocation_bytes"] == joined["device_allocation_bytes"]
        reports.append(
            dict(
                device=device,
                baseline=baseline,
                observations=observations,
                joined=joined,
                diagnostics=session.diagnostics(),
                viewers=0,
            )
        )
    assert session.status()["device_allocation_bytes"] == 0
destination = artifacts_root() / "stage1/progressive-buffer-probe.json"
destination.write_text(json.dumps(dict(result="passed", reports=reports), indent=2), encoding="utf-8")
print(destination)
