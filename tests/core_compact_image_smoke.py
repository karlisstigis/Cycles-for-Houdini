# SPDX-License-Identifier: Apache-2.0
"""Independent compact plane placement, storage and strict wire contracts."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import argparse
import json
import os
from pathlib import Path
import subprocess
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--compact-image-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=60,
)
assert run.returncode == 0, (run.stdout, run.stderr)
from worker_protocol_fixture import expected_worker_protocol

report = json.loads(run.stdout.splitlines()[-1])
assert report["checks"] >= 185 and report["worker_protocol"] == expected_worker_protocol()
owned = ch._native.compact_frame_fixture()
info = ch._native.frame_info(owned)
raw = ch._native.frame(owned)
rgba = np.asarray(
    [
        [0.1, 0.2, 0.3, 1],
        [0.2, 0.1, 0, 0.5],
        [0.4, 0, 0.2, 0],
        [0, 0.3, 0.1, 1],
        [0.15, 0.1, 0.05, 0.25],
        [0.4, 0.3, 0.2, 1],
    ],
    np.float32,
)
compact = {
    "combined": rgba.reshape(2, 3, 4),
    "noisy_combined": rgba.reshape(3, 2, 4),
    "depth": np.asarray([0.125, 0.25, 0.5, 1], np.float32).reshape(2, 2, 1),
    "normal": np.asarray([1, 0, 0, 0, 1, 0, 0, 0, 1, 0.2, 0.4, 0.6, 0.8, 0.6, 0.4, 0, 1, 1], np.float32).reshape(
        3, 2, 3
    ),
}
checks = []
for name, pixels in compact.items():
    h, w, c = pixels.shape
    assert info["plane_storage"][name] == dict(width=w, height=h, bytes=pixels.nbytes)
    expected = pixels[np.arange(5) * h // 5][:, np.arange(7) * w // 7]
    actual = np.frombuffer(raw["planes"][name]["pixels"], np.float32).reshape(5, 7, c)
    assert np.array_equal(actual, expected), name
    displayed = ch._native.display_image(owned, {"pass": name, "raw": True})
    rgb = expected[..., :3].copy() if c >= 3 else np.repeat(expected, 3, axis=2)
    alpha = expected[..., 3] if c == 4 else np.ones((5, 7), np.float32)
    if c == 4:
        np.divide(rgb, alpha[..., None], out=rgb, where=alpha[..., None] != 0)
    packed = np.concatenate([rgb, alpha[..., None]], axis=2)
    expected_bytes = np.rint(np.clip(packed, 0, 1) * 255).astype(np.uint8)[::-1]
    actual_bytes = np.frombuffer(displayed["rgba"], np.uint8).reshape(5, 7, 4)
    assert np.abs(actual_bytes.astype(np.int16) - expected_bytes.astype(np.int16)).max() <= 1, name
    assert (displayed["x"], displayed["y"], displayed["width"], displayed["height"]) == (2, 3, 7, 5)
    checks.append(name + " independent storage/raw/display placement")
assert info["pixel_bytes"] == 376 and (info["full_width"], info["full_height"]) == (13, 11)
report.update(device=device, python_checks=checks, pixel_bytes=info["pixel_bytes"])
destination = artifacts_root() / "stage1/compact-images"
destination.mkdir(parents=True, exist_ok=True)
(destination / "core-compact-image-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
