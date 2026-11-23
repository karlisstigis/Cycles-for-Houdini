# SPDX-License-Identifier: Apache-2.0
"""Five cancel/restart cycles retain one million-triangle packed scene and finish a valid image."""

import json
import os
from pathlib import Path
import sys
from support.devices import fixture_device
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from packed_deformation_fixture import Fixture

device = fixture_device(sys.argv)
f = Fixture(device)
f.motion.parm("test_rows").set(721)
f.targets.setParms(dict(rows=4, cols=4))
f.targets.parmTuple("size").set((6, 6))
rop = f.rop(f.nested)
rop.setParms(dict(samples=1048576, ipr_samples=1048576, allowmotionblur=0))
path = f.dest / "cancel-restart-report.json"
report = dict(result="running", scope=__doc__, device=device, events=[], prototype_triangles=1036800, instances=16)

try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1048576,
        interactive=True,
        execution="sync",
        collect_diagnostics=True,
        threads=2,
    ) as session:
        update = native_scene.capture(rop)
        conversions = None
        for number in range(5):
            generation = session.apply(update if number == 0 else dict(camera_path=f.camera.path()), at_time=0)
            began = time.monotonic()
            while True:
                metadata = session.frame_info()
                status = session.status()
                assert status["state"] != "failed", status
                if (
                    metadata
                    and metadata["generation"] == generation
                    and metadata.get("current_scene", True)
                    and not metadata["complete"]
                ):
                    break
                assert time.monotonic() - began < 60, status
                time.sleep(0.005)
            assert status["geometry_resources"] == 1 and status["object_resources"] == 16, status
            if conversions is None:
                conversions = status["geometry_conversions"]
            assert status["geometry_conversions"] == conversions, status
            began = time.monotonic()
            session.cancel()
            elapsed = time.monotonic() - began
            assert elapsed < 2.0, elapsed
            assert session.status()["state"] == "cancelled"
            report["events"].append(
                dict(cancel_seconds=elapsed, status=session.status(), diagnostics=session.diagnostics())
            )
            f.camera.parm("tx").set((number + 1) * 0.02)
        generation = session.apply(dict(camera_path=f.camera.path(), rendering=dict(samples=8)), at_time=0)
        assert session.status()["rendering"]["samples"] == 8
        image = session.wait(generation)
        assert image["complete"] and np.any(f.pixels(image)[:, :, :3] > 0.1)
        assert abs(f.coverage(image, -1 - f.camera.evalParm("tx"), -1) - 1) < 0.025
        assert session.status()["geometry_conversions"] == conversions
    closed = session.status()
    assert closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, (
        closed
    )
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", cycles=len(report["events"]))))
