# SPDX-License-Identifier: Apache-2.0
"""Independent stock Cycles lookup over 256 intervals and an endpoint.

The table comes from a public host evaluator (Houdini or pinned Blender),
never the product's ramp preparer. This models sampling loss deliberately.
"""

import numpy as np


def lookup(table, factor, interpolate=True, extrapolate=False):
    table = np.asarray(table, np.float32)
    x = np.float32(factor)
    if extrapolate and (x < 0 or x > 1):
        if x < 0:
            return table[0] + (table[0] - table[1]) * (-x * np.float32(256))
        return table[-1] + (table[-1] - table[-2]) * ((x - np.float32(1)) * np.float32(256))
    index = np.float32(np.clip(x, 0, 1)) * np.float32(256)
    i = int(index)
    if not interpolate or i == 256:
        return table[i]
    weight = np.float32(index - i)
    return (np.float32(1) - weight) * table[i] + weight * table[i + 1]


def native_lookup(ramp, factor, interpolate=None):
    if interpolate is None:
        interpolate = not all(str(b).endswith("Constant") for b in ramp.basis())
    return lookup([ramp.lookup(i / 256) for i in range(257)], factor, interpolate)
