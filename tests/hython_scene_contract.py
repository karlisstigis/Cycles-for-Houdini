# SPDX-License-Identifier: Apache-2.0
"""Small real native scene in synchronous and worker modes on one device."""

import argparse
import json
from pathlib import Path

import cycles_houdini as ch
import hou

from support.scenes import box_scene, check_box_pixels

parser = argparse.ArgumentParser()
parser.add_argument("--device", required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
devices = ch.devices()
device = next((item["id"] for item in devices if item["backend"] == args.device), None)
assert device is not None, ("Required backend is unavailable", args.device, devices)
assert ch.capabilities()["image_iris"] and ch.capabilities()["gaussian_splats"]
records = []
with box_scene() as (geometry, box, camera):
    update = dict(
        geometry=[ch.geometry_request(geometry, box, material="red")],
        camera_path=camera.path(),
        background=(0.05, 0.05, 0.05),
        materials=[dict(key="red", kind="diffuse", color=(0.8, 0.05, 0.025))],
    )
    for mode in ("sync", "worker"):
        with ch.Session(device=device, width=32, height=24, samples=2, execution=mode) as session:
            generation = session.apply(update, hou.time())
            frame = session.wait(generation, timeout=90)
            assert frame["complete"] and frame["generation"] == generation
            pixels = frame["planes"]["combined"]["pixels"]
            assert len(pixels) == 32 * 24 * 4 * 4
            colors = check_box_pixels(pixels)
            records.append(dict(mode=mode, generation=generation, state=session.status()["state"], **colors))
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(dict(result="passed", device=device, records=records), indent=2))
