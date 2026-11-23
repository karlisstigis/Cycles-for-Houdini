# SPDX-License-Identifier: Apache-2.0
"""Require actual unfinished image updates, safe cancellation, and crop coordinates."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import time

import cycles_houdini as ch


def unfinished(session, generation, timeout=30):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        status = session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        frame = session.frame()
        if frame and frame["generation"] == generation:
            assert 0 < frame["rendered_width"] <= frame["width"] and 0 < frame["rendered_height"] <= frame["height"]
            assert frame["published_seconds"] > 0
            assert not frame["complete"], "Only a finished output arrived; progressive display failed"
            assert status["progress"] < 1, "Progressive image arrived after all samples"
            return frame, status
        time.sleep(0.002)
    raise TimeoutError("No progressive frame before the deadline")


reports = []
gpu = ch.default_device()
for device in ("CPU", gpu):
    with ch.Session(device=device, width=320, height=180, samples=65536) as session:
        generation = session.calibration()
        frame, status = unfinished(session, generation)
        assert (frame["full_width"], frame["full_height"]) == (320, 180)
        assert len(frame["planes"]["combined"]["pixels"]) == 320 * 180 * 4 * 4
        session.open_native_display("null:")
        session.present_native()
        session.pause()
        assert session.status()["state"] == "paused"
        session.pause(False)
        cropped_generation = session.apply({"crop": (80, 45, 160, 90)})
        cropped, crop_status = unfinished(session, cropped_generation)
        assert (
            cropped["x"],
            cropped["y"],
            cropped["width"],
            cropped["height"],
            cropped["full_width"],
            cropped["full_height"],
        ) == (80, 45, 160, 90, 320, 180), cropped
        assert session.status()["geometry_conversions"] == 1
        session.present_native()
        session.cancel()
        assert session.status()["state"] == "cancelled"
        sequence = session.frame()["sequence"]
        cancelled_elapsed = session.status()["elapsed_seconds"]
        time.sleep(0.05)
        assert session.frame()["sequence"] == sequence, "Frames arrived after cancellation joined the renderer"
        assert session.status()["elapsed_seconds"] == cancelled_elapsed, (
            "Cancelled render elapsed time continues to increase"
        )
        reports.append(
            dict(
                device=device,
                progressive=status,
                cropped=crop_status,
                crop=[cropped[k] for k in ("x", "y", "width", "height", "full_width", "full_height")],
            )
        )
    assert session.status()["device_allocation_bytes"] == 0

destination = artifacts_root() / "stage1/houdini-progressive-smoke.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(dict(result="passed", devices=reports), indent=2), encoding="utf-8")
print(destination)
