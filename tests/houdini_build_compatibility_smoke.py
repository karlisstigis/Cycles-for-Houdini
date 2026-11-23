# SPDX-License-Identifier: Apache-2.0
"""Focused per-SDK native load, worker, image and clean-exit qualification."""

from array import array
import json
import math
from pathlib import Path
import sys

import hou
import cycles_houdini as ch
from support.paths import artifacts_root


device = sys.argv[sys.argv.index("--device") + 1]
backend = "CPU" if device == "CPU" else "OPTIX"
assert ch.capabilities()["image_iris"]
assert any(item["id"] == device for item in ch.devices())

obj = hou.node("/obj")
geo = obj.createNode("geo", "compatibility_geometry", run_init_scripts=False)
box = geo.createNode("box", "source")
box.setRenderFlag(True)
camera = obj.createNode("cam", "compatibility_camera")
camera.parmTuple("t").set((0, 0, 5))

session = ch.Session(device=device, width=64, height=64, samples=8, interactive=False, execution="worker")
process = None
try:
    generation = session.apply(
        dict(
            geometry=[ch.geometry_request(geo, box, material="glow")],
            camera_path=camera.path(),
            materials=[dict(key="glow", kind="emission", color=(0.8, 0.2, 0.1))],
            background=(0.01, 0.01, 0.01),
        )
    )
    image = session.wait(generation, timeout=120)
    status = session.status()
    assert status["state"] == "complete" and status["render_device_ids"] == [device], status
    pixels = array("f")
    pixels.frombytes(image["planes"]["combined"]["pixels"])
    assert len(pixels) == 64 * 64 * 4
    center = tuple(pixels[(32 * 64 + 32) * 4 : (32 * 64 + 32) * 4 + 3])
    corner = tuple(pixels[:3])
    assert all(math.isfinite(value) for value in pixels)
    assert center[0] > 0.3 and center[0] > center[1] * 2 and center[1] > center[2], center
    assert max(corner) < 0.1, corner
    assert session._controller.backend is not None
    process = session._controller.backend.process
    assert process.poll() is None
finally:
    session.close(wait=True)
assert process is not None and process.poll() == 0, "Worker did not exit cleanly"

path = artifacts_root() / "compatibility" / f"houdini-build-smoke-{backend.lower()}.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    json.dumps(
        dict(
            result="passed",
            houdini=hou.applicationVersionString(),
            device=device,
            actual_device_ids=status["render_device_ids"],
            backend=backend,
            native_module="loaded",
            worker_handshake="passed",
            image="passed",
            center_rgb=center,
            corner_rgb=corner,
            process_exit_code=process.returncode,
        ),
        indent=2,
    ),
    encoding="utf-8",
)
