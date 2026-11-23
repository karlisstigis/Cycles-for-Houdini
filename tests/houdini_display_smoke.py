# SPDX-License-Identifier: Apache-2.0
"""Check display color independently with OCIO, and the documented tile-device route."""

from support.paths import artifacts_root
from array import array
import json
import math
import os
import time
from pathlib import Path

import PyOpenColorIO as OCIO
import cycles_houdini as ch


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1"
destination.mkdir(parents=True, exist_ok=True)
policy = ch.color_policy()
config = OCIO.GetCurrentConfig()
assert policy["working_space"] == config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
display = policy["default_display"]
view = policy["default_views"][display]
processor = config.getProcessor(
    OCIO.ROLE_SCENE_LINEAR, display, view, OCIO.TRANSFORM_DIR_FORWARD
).getDefaultCPUProcessor()

with ch.Session(device="CPU", width=320, height=180, samples=16) as session:
    generation = session.calibration()
    frame = session.wait(generation)
    metadata = session.frame_info()
    assert metadata["rendered_width"] == frame["width"] == 320 and metadata["rendered_height"] == frame["height"] == 180
    assert frame["published_seconds"] == metadata["published_seconds"] == session.status()["elapsed_seconds"]
    finished_elapsed = session.status()["elapsed_seconds"]
    time.sleep(0.05)
    assert session.status()["elapsed_seconds"] == finished_elapsed, (
        "Completed render elapsed time continues to increase"
    )
    floats = array("f")
    floats.frombytes(frame["planes"]["combined"]["pixels"])
    image = session.display_image(display=display, view=view)
    bright = session.display_image(display=display, view=view, exposure=1)
    assert session.status()["generation"] == generation
    assert image["rgba"] != bright["rgba"]
    # The reference uses the public OCIO Python binding and explicit source pixels.
    checked = 0
    for x, y in [(0, 0), (160, 90), (160, 70), (100, 60), (230, 130)]:
        source = (y * frame["width"] + x) * 4
        rgba = list(floats[source : source + 4])
        alpha = rgba[3]
        rgba[:3] = [v / alpha if alpha > 1e-8 else 0 for v in rgba[:3]]
        expected = processor.applyRGBA(rgba)
        target = ((frame["height"] - 1 - y) * frame["width"] + x) * 4
        expected_bytes = [round(max(0, min(1, v if math.isfinite(v) else 0)) * 255) for v in expected]
        assert all(abs(image["rgba"][target + c] - expected_bytes[c]) <= 1 for c in range(4))
        checked += 1
    # Data channels must bypass exposure and display transforms.
    normal = session.display_image(**{"pass": "normal"})
    normal_bright = session.display_image(**{"pass": "normal", "exposure": 4, "view": "Raw"})
    assert normal["rgba"] == normal_bright["rgba"]
    session.open_native_display("null:")
    session.present_native()
    session.present_native()  # Same frame is a harmless no-op.
    cropped_generation = session.apply({"crop": (80, 45, 160, 90)})
    cropped = session.wait(cropped_generation)
    assert (cropped["x"], cropped["y"], cropped["width"], cropped["height"]) == (80, 45, 160, 90)
    session.present_native()
    session.close_native_display()
    session.close_native_display()
    session.write_exr(destination / "crop.exr")
    status = session.status()

report = dict(
    color_policy=policy,
    reference_pixels=checked,
    display_only_edit="generation unchanged",
    data_channels="raw",
    tile_device="null device lifecycle passed; live IPR is covered by the separate GUI tests",
    crop="passed",
    status=status,
)
(destination / "houdini-display-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
