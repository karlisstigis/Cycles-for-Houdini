# SPDX-License-Identifier: Apache-2.0
"""Independent compact coverage/data/raw expectations, including fractional alpha."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path

import numpy as np
import cycles_houdini as ch

owned = ch._native.compact_frame_fixture()
rgba = np.asarray(
    [
        (0.1, 0.2, 0.3, 1),
        (0.2, 0.1, 0, 0.5),
        (0.4, 0, 0.2, 0),
        (0, 0.3, 0.1, 1),
        (0.15, 0.1, 0.05, 0.25),
        (0.4, 0.3, 0.2, 1),
    ],
    np.float32,
)
normal = np.asarray([(1, 0, 0), (0, 1, 0), (0, 0, 1), (0.2, 0.4, 0.6), (0.8, 0.6, 0.4), (0, 1, 1)], np.float32)
depth = np.asarray([0.125, 0.25, 0.5, 1], np.float32).reshape(2, 2, 1)
cases = []


def expected(source, width, height):
    source = source.reshape(height, width, 4)
    rows = np.arange(5) * height // 5
    columns = np.arange(7) * width // 7
    # Logical render coordinates start at the bottom. Expand there before
    # converting the final logical raster to top-down display rows.
    values = source[rows[:, None], columns][::-1]
    return np.floor(np.clip(np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0), 0, 1) * 255 + 0.5).astype(np.uint8)


def check(name, settings, source, width, height):
    image = ch._native.display_image(owned, settings)
    actual = np.frombuffer(image["rgba"], np.uint8).reshape(5, 7, 4)
    wanted = expected(source, width, height)
    difference = int(np.abs(actual.astype(np.int16) - wanted.astype(np.int16)).max())
    assert difference <= 1, (name, difference, actual.tolist(), wanted.tolist())
    assert np.array_equal(actual[..., 3], wanted[..., 3]), (name, "Coverage changed")
    assert (image["x"], image["y"], image["full_width"], image["full_height"]) == (2, 3, 13, 11)
    assert abs(image["pixel_aspect"] - 1.6) < 1e-6
    cases.append(
        dict(
            name=name,
            maximum_byte_error=difference,
            alpha_values=np.unique(actual[..., 3]).tolist(),
            rgb_values=np.unique(actual[..., 0]).tolist(),
        )
    )


alpha = np.repeat(rgba[:, 3, None], 4, axis=1)
alpha[:, 3] = 1
check("Fractional coverage is shown as grayscale", dict(channels="alpha"), alpha, 3, 2)
check(
    "Coverage ignores exposure, gamma and display transforms",
    dict(channels="alpha", exposure=3.0, gamma=2.2),
    alpha,
    3,
    2,
)
raw = rgba.copy()
mask = rgba[:, 3] != 0
raw[mask, :3] /= rgba[mask, 3, None]
check("Raw RGB is unpremultiplied while zero-coverage additive RGB is retained", dict(raw=True), raw, 3, 2)
check(
    "A pass with different compact dimensions retains its own row layout",
    {"pass": "noisy_combined", "raw": True},
    raw,
    2,
    3,
)
raw_rgb = raw.copy()
raw_rgb[:, 3] = 1
check("Raw RGB selection is opaque", dict(raw=True, channels="rgb"), raw_rgb, 3, 2)
adjusted = raw.copy()
adjusted[:, :3] *= np.float32(2**0.75)
adjusted[:, :3] = np.maximum(adjusted[:, :3], 0) ** np.float32(1 / 1.4)
check("Raw exposure and gamma retain coverage", dict(raw=True, exposure=0.75, gamma=1.4), adjusted, 3, 2)
data = np.concatenate((normal, np.ones((6, 1), np.float32)), axis=1)
check("Normal data is opaque and ignores color controls", {"pass": "normal", "exposure": 3.0, "gamma": 2.2}, data, 2, 3)
data = np.concatenate((np.repeat(depth, 3, axis=2), np.ones((2, 2, 1), np.float32)), axis=2)
check(
    "Scalar data uses grayscale and ignores color controls",
    {"pass": "depth", "exposure": -3.0, "gamma": 2.2},
    data,
    2,
    2,
)

ids = np.asarray([0x3F812345, 0x7FC54321, 0x80000000, 0x01234567, 0x76543210, 0xFFC12345], np.uint32)
crypto = np.zeros((6, 4), np.float32)
crypto[:, 3] = 1
for i, value in enumerate(ids):
    for code, coverage in ((int(value), 0.75), (int(value) ^ 0x12345, 0.25)):
        for c in range(3):
            crypto[i, c] += np.float32(coverage * (0.2 + 0.8 * ((code >> (c * 8)) & 255) / 255))
check(
    "Cryptomatte IDs remain false colors, including NaN bit patterns",
    {"pass": "crypto_object00", "exposure": 3.0, "gamma": 2.2},
    crypto,
    3,
    2,
)
try:
    ch._native.display_image(owned, {"pass": "crypto_object00", "channels": "alpha"})
except RuntimeError as error:
    assert "Cryptomatte" in str(error)
else:
    raise AssertionError("Cryptomatte ID/coverage pairs were treated as alpha")

report = dict(
    result="passed",
    checks=cases,
    scope="Known independent compact source arrays, odd nearest expansion, orientation/crop/aspect metadata and byte quantization. No live renderer or latency claim.",
)
path = artifacts_root() / "stage1/display-profile/data-reference-report.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(path)
