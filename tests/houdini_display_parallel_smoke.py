# SPDX-License-Identifier: Apache-2.0
"""Compare the tiled display path with an independently applied OCIO processor."""

from support.paths import artifacts_root
import json, os
from pathlib import Path
import numpy as np
import PyOpenColorIO as ocio
import cycles_houdini as ch

report = dict(result="running", cases=[])
with ch.Session(device="CPU", width=257, height=257, samples=4, interactive=False) as session:
    frame = session.wait(session.calibration())
    policy = ch.color_policy()
    processor = (
        ocio.GetCurrentConfig()
        .getProcessor(
            policy["working_space"],
            policy["default_display"],
            policy["default_views"][policy["default_display"]],
            ocio.TRANSFORM_DIR_FORWARD,
        )
        .getDefaultCPUProcessor()
    )
    for plane, channels in (("combined", "rgba"), ("combined", "rgb"), ("combined", "alpha"), ("normal", "rgba")):
        data = frame["planes"][plane]
        source = np.frombuffer(data["pixels"], np.float32).reshape(257, 257, data["channels"])
        alpha = source[:, :, 3] if data["channels"] == 4 else np.ones((257, 257), np.float32)
        if channels == "alpha":
            rgb = np.repeat(alpha[:, :, None], 3, axis=2)
        else:
            rgb = source[:, :, :3].copy()
            if plane == "combined":
                np.divide(rgb, alpha[:, :, None], out=rgb, where=alpha[:, :, None] != 0)
                rgb *= np.float32(2**0.75)
                processor.applyRGB(rgb)
        expected = np.concatenate((rgb, (alpha if channels == "rgba" else np.ones_like(alpha))[:, :, None]), axis=2)
        expected = np.rint(np.clip(np.nan_to_num(expected, nan=0), 0, 1) * 255).astype(np.uint8)[::-1]
        displayed = session.display_image(**{"pass": plane, "channels": channels, "exposure": 0.75})
        actual = np.frombuffer(displayed["rgba"], np.uint8).reshape(257, 257, 4)
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert difference.max() <= 1, (plane, channels, difference.max())
        assert np.array_equal(actual[:, :, 3], expected[:, :, 3]), (plane, channels, "coverage changed")
        report["cases"].append(dict(plane=plane, channels=channels, max_byte_error=int(difference.max())))
report["result"] = "passed"
report["closed"] = session.status()
assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
destination = artifacts_root() / "stage1/output/houdini-display-parallel-smoke.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
