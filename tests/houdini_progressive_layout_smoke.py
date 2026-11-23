# SPDX-License-Identifier: Apache-2.0
"""Every navigation preview must retain independently specified image geometry."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini.viewer.panel import ImageCanvas
from cycles_houdini.viewer.viewer_pick import display_value

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
if device == "OPTIX_ALL":
    device = [value["id"] for value in ch.devices() if value["backend"] == "OPTIX"]
    assert len(device) >= 2, "Multi-device layout test requires two OptiX devices"
destination = artifacts_root() / "stage1/progressive-layout"
destination.mkdir(parents=True, exist_ok=True)
report = dict(
    result="running",
    device=device,
    cases=[],
    scope="Independent emission quadrants during progressive rendering, camera updates and an odd-sized crop. Checks image contents, not just dimensions or completion.",
)
camera = hou.node("/obj").createNode("cam", "layout_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=6))
colors = ((0.8, 0.1, 0.05), (0.05, 0.7, 0.1), (0.05, 0.1, 0.8), (0.7, 0.5, 0.1))
geometry = []
materials = []
for index, ((x, y), color) in enumerate(zip(((-2, -2), (2, -2), (-2, 2), (2, 2)), colors)):
    obj = hou.node("/obj").createNode("geo", "quadrant_" + str(index), run_init_scripts=False)
    grid = obj.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((4, 4))
    obj.parmTuple("t").set((x, y, 0))
    geometry.append(ch.geometry_request(obj, grid, material="color_" + str(index)))
    materials.append(dict(key="color_" + str(index), kind="emission", color=color, strength=1))


def check(frame, camera_x, full_size):
    assert not frame["complete"], "Test must inspect navigation previews"
    w, h = frame["width"], frame["height"]
    x, y = frame["x"], frame["y"]
    for name, plane in frame["planes"].items():
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(h, w, plane["channels"])
        assert np.isfinite(pixels).all(), name
        if name in ("combined", "noisy_combined"):
            # The four independently authored surfaces cover the entire camera.
            # Unwritten/repeated allocation rows must not appear transparent.
            assert np.min(pixels[:, :, 3]) > 0.999, (name, "coverage", float(np.min(pixels[:, :, 3])))
            for u, v in ((0.15, 0.2), (0.3, 0.8), (0.75, 0.2), (0.85, 0.8)):
                ix, iy = int(u * w), int(v * h)
                world_x = ((x + ix + 0.5) / full_size[0] - 0.5) * 6 + camera_x
                world_y = ((y + iy + 0.5) / full_size[1] - 0.5) * 6 * full_size[1] / full_size[0]
                expected = colors[int(world_x > 0) + 2 * int(world_y > 0)]
                actual = pixels[iy - 2 : iy + 3, ix - 2 : ix + 3, :3].mean(axis=(0, 1))
                assert np.allclose(actual, expected, atol=0.018, rtol=0), (name, u, v, actual.tolist(), expected)
        elif name == "normal":
            assert np.allclose(pixels, (0, 0, 1), atol=0.001, rtol=0), ("normal layout", float(pixels[:, :, 2].min()))


def check_display(owned, frame, name, previous):
    settings = {"pass": name, "backgrounds": ((48, 48, 48), (64, 64, 64))}
    old = ch._native.display_image(owned, settings)
    candidate = ch._native.display_image(owned, dict(settings, compact_full_frame=True))
    assert (candidate["x"], candidate["y"], candidate["width"], candidate["height"]) == (
        old["x"],
        old["y"],
        old["width"],
        old["height"],
    )
    width, height = frame["width"], frame["height"]
    full_size = frame["full_width"], frame["full_height"]
    plane = ch._native.frame_info(owned)["plane_storage"][name]
    storage = plane["width"], plane["height"]
    compact = (
        not (frame["x"] or frame["y"])
        and (width, height) == full_size
        and storage != full_size
        and width % storage[0] == 0
        and height % storage[1] == 0
    )
    if not compact:
        assert candidate == old
        if previous is not None:
            actual, expected = (ImageCanvas.merge_frame(base, candidate) for base in previous)
            assert bytes(actual.constBits()) == bytes(expected.constBits())
        return None

    assert (candidate["buffer_width"], candidate["buffer_height"]) == storage
    assert len(candidate["rgba"]) == storage[0] * storage[1] * 4
    image = ImageCanvas.merge_frame(None, candidate)
    expected = ImageCanvas.merge_frame(None, old)
    exported = ImageCanvas.full_resolution_copy(image)
    assert image.size().toTuple() == storage and ImageCanvas.logical_size(image) == full_size
    assert exported.size().toTuple() == full_size and bytes(exported.constBits()) == bytes(expected.constBits())
    for x, y in ((0, 0), (width - 1, height - 1), (width // 2, height // 2)):
        assert display_value(image, x, y, full_size) == display_value(expected, x, y)
    # Scene-linear checker layers share the compact geometry and export with the same orientation.
    layers = candidate.get("composites", ())
    assert len(layers) == (2 if name == "combined" else 0)
    for layer, expected_layer in zip(layers, old.get("composites", ()), strict=True):
        assert ch._native.expand_display_rgba(layer, *storage, *full_size) == expected_layer
    return image, expected


def check_compact_display_fixture():
    # The live renderer may replace its low-resolution frame before the worker
    # transfers it. Exercise the compact Beauty conversion independently.
    owned = ch._native.compact_frame_fixture(True)
    frame = ch._native.frame(owned)
    images = check_display(owned, frame, "combined", None)
    assert images is not None
    full_size = frame["full_width"], frame["full_height"]
    storage = images[0].size().toTuple()
    settings = {"pass": "combined", "compact_full_frame": True}
    plan = ch._native.display_gpu_input(owned, settings)
    assert plan["compact"] and (plan["buffer_width"], plan["buffer_height"]) == storage
    assert len(plan["pixels"]) == storage[0] * storage[1] * 16

    cropped = ch._native.compact_frame_fixture()
    crop = ch._native.display_image(cropped, settings)
    assert "buffer_width" not in crop
    assert not ch._native.display_gpu_input(cropped, settings).get("compact", False)
    # The existing cropped fixture uses its own raster. Retarget its local
    # tile to the integral fixture's raster to check the retained background.
    crop["full_width"], crop["full_height"] = full_size
    merged = [ImageCanvas.merge_frame(image, crop) for image in images]
    assert bytes(merged[0].constBits()) == bytes(merged[1].constBits())
    outside = display_value(merged[0], 0, 0)
    assert outside == display_value(images[0], 0, 0, full_size)

    tile = ImageCanvas.merge_frame(None, dict(crop, x=0, y=0, full_width=crop["width"], full_height=crop["height"]))
    # This logical pixel selects different source columns in the retained
    # integral image and the non-integral crop.
    point = crop["x"] + 2, crop["y"]
    replaced = display_value(merged[0], *point)
    assert replaced == display_value(tile, 2, 0)
    assert replaced != display_value(images[0], *point, full_size)
    report["compact_display_fixture"] = dict(
        logical_size=full_size, storage=storage, crop_merge=True, gpu_plan=True, outside=outside, replaced=replaced
    )


try:
    check_compact_display_fixture()
    for denoise, preview in ((False, "combined"), (True, "combined"), (False, "normal"), (True, "normal")):
        full_size = (640, 360) if not denoise and preview == "combined" else (641, 361)
        row = dict(denoise=denoise, preview=preview, full_size=full_size, frames=[])
        report["cases"].append(row)
        camera.parm("tx").set(0)
        with ch.Session(
            device=device,
            width=full_size[0],
            height=full_size[1],
            samples=1048576,
            passes=["combined", "normal"],
            collect_diagnostics=True,
            denoising=dict(enabled=denoise, method="auto", use_gpu=True),
        ) as session:
            session.set_preview_pass(preview)
            previous_display = None
            for edit, camera_x in enumerate((0, 0.15, -0.1)):
                camera.parm("tx").set(camera_x)
                update = dict(camera_path=camera.path())
                if edit == 0:
                    update.update(geometry=geometry, materials=materials, background=(0, 0, 0))
                if edit == 2:
                    update["crop"] = (13, 19, 509, 281)
                target = session.apply(update)
                started = time.monotonic()
                after = 0
                count = 0
                checked_sizes = set()
                while time.monotonic() - started < 20:
                    status = session.status()
                    if status["state"] == "failed":
                        raise RuntimeError(status["error"])
                    owned = session.display_frame(after)
                    frame = ch._native.frame(owned[0]) if owned else None
                    if frame and frame["generation"] == target:
                        after = frame["sequence"]
                        count += 1
                        row["frames"].append(
                            {
                                key: frame[key]
                                for key in (
                                    "generation",
                                    "sequence",
                                    "width",
                                    "height",
                                    "rendered_width",
                                    "rendered_height",
                                    "samples",
                                )
                            }
                        )
                        try:
                            check(frame, camera_x, full_size)
                            size = frame["rendered_width"], frame["rendered_height"]
                            if size not in checked_sizes and preview in frame["planes"]:
                                checked_sizes.add(size)
                                pair = check_display(owned[0], frame, preview, previous_display if edit == 2 else None)
                                if pair is not None:
                                    previous_display = pair
                                    row["compact_viewer_preview"] = True
                                    row["frames"][-1]["compact_storage"] = pair[0].size().toTuple()
                        except BaseException:
                            pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(
                                frame["height"], frame["width"], 4
                            )
                            output = OIIO.ImageOutput.create(str(destination / "failed-preview.exr"))
                            output.open(
                                str(destination / "failed-preview.exr"),
                                OIIO.ImageSpec(frame["width"], frame["height"], 4, OIIO.FLOAT),
                            )
                            output.write_image(pixels)
                            output.close()
                            raise
                        if count >= 3 and time.monotonic() - started > 0.25:
                            break
                    time.sleep(0.002)
                assert count >= 3, ("Not enough progressive observations", count)
            session.cancel()
            row["diagnostics"] = session.diagnostics()
    assert any(f["rendered_width"] < f["width"] for row in report["cases"] for f in row["frames"]), (
        "No reduced-resolution preview was exercised"
    )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path = destination / "houdini-progressive-layout-smoke.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(path)
