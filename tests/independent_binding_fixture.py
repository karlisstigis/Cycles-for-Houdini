# SPDX-License-Identifier: Apache-2.0
"""Analytic box-shutter coverage, independent of the scene translator."""

import numpy as np


def motion_pixels(centres, shift=0):
    x = (np.arange(160) + 0.5 - 80) / 40
    coverage = np.zeros(160)
    for start, end in zip(centres, centres[1:]):
        lo, hi = sorted((start, end))
        coverage += (
            (np.abs(x - start) < 0.75)
            if lo == hi
            else np.maximum(0, np.minimum(hi, x + 0.75) - np.maximum(lo, x - 0.75)) / (hi - lo)
        )
    expected = np.zeros((90, 160, 3), np.float32)
    expected[25:65] = coverage[None, :, None] / (len(centres) - 1) * np.asarray((0.2, 0.4, 0.8))[None, None, :]
    expected = np.roll(expected, shift, axis=1)
    if shift < 0:
        expected[:, shift:] = 0
    elif shift > 0:
        expected[:, :shift] = 0
    return expected
