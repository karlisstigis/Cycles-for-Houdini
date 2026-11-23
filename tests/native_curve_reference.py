# SPDX-License-Identifier: Apache-2.0
"""Independent references for inline native-ramp Float/RGB/Vector Curves.

Positions 0..1 span Range; values are output values. Bake 257 samples from
hou.Ramp.lookup, composing the authored RGB channels before sampling. Inside
Range use stock linear interpolation; outside use its endpoint table chords.
The authored smooth endpoint slope uses independent Richardson differences.
Factor blends unclamped.
"""

import hou, numpy as np
from functools import lru_cache
from ramp_table_reference import lookup

BASES = ("constant", "linear", "catmull_rom", "monotone_cubic", "bezier", "bspline", "hermite")
HOU = {
    "constant": hou.rampBasis.Constant,
    "linear": hou.rampBasis.Linear,
    "catmull_rom": hou.rampBasis.CatmullRom,
    "monotone_cubic": hou.rampBasis.MonotoneCubic,
    "bezier": hou.rampBasis.Bezier,
    "bspline": hou.rampBasis.BSpline,
    "hermite": hou.rampBasis.Hermite,
}
CHANNELS = {"float_curve": ("value",), "rgb_curves": ("c", "r", "g", "b"), "vector_curves": ("x", "y", "z")}


def ramp(channel):
    bases, keys, values = channel
    return hou.Ramp(tuple(HOU[b] for b in bases), tuple(keys), tuple(values))


def _slope(channel, start):
    bases, keys, values = channel
    if len(keys) < 2:
        return 0.0
    a, b = (0, 1) if start else (len(keys) - 2, len(keys) - 1)
    basis = bases[a]
    if basis == "constant":
        return 0.0
    if basis == "linear":
        return (values[b] - values[a]) / (keys[b] - keys[a])
    # Richardson-extrapolated second-order one-sided differences (an
    # independent formula from the renderer's local cubic fit).
    r = ramp(channel)
    origin, direction = (keys[a], 1.0) if start else (keys[b], -1.0)

    def difference(h):
        f = lambda n: r.lookup(origin + direction * n * h)
        return direction * (-3 * f(0) + 4 * f(1) - f(2)) / (2 * h)

    h = (keys[b] - keys[a]) * 0.01
    return (4 * difference(h / 2) - difference(h)) / 3


def channel_map(channel, x, domain, extrapolate):
    t = (x - domain[0]) / (domain[1] - domain[0])
    keys = channel[1]
    key = tuple(tuple(part) for part in channel)
    y = channel_ramp(key).lookup(min(max(t, 0.0), 1.0))
    if extrapolate:
        if t < keys[0]:
            y += (t - keys[0]) * _slope(channel, True)
        if t > keys[-1]:
            y += (t - keys[-1]) * _slope(channel, False)
    return y


@lru_cache(maxsize=128)
def channel_ramp(channel):
    return ramp(channel)


def _authored(kind, channels, domain, extrapolate, value):
    """channels: one (bases, keys, values) per channel in node order."""
    if kind == "float_curve":
        curve = channel_map(channels[0], value, domain, extrapolate)
        return curve
    value = np.asarray(value, float)
    if kind == "rgb_curves":
        curve = [
            channel_map(channels[1 + i], channel_map(channels[0], value[i], domain, extrapolate), domain, extrapolate)
            for i in range(3)
        ]
    else:
        curve = [channel_map(channels[i], value[i], domain, extrapolate) for i in range(3)]
    return np.asarray(curve)


@lru_cache(maxsize=128)
def curve_table(kind, channels, domain, extrapolate):
    return np.asarray(
        [
            _authored(
                kind,
                channels,
                domain,
                extrapolate,
                domain[0] + i / 256 * (domain[1] - domain[0])
                if kind == "float_curve"
                else (domain[0] + i / 256 * (domain[1] - domain[0]),) * 3,
            )
            for i in range(257)
        ],
        np.float32,
    )


def evaluate(kind, channels, domain, extrapolate, value, factor=1.0):
    """Bake independently, then apply stock curve interpolation/extrapolation."""
    channels = tuple(tuple(tuple(part) for part in channel) for channel in channels)
    table = curve_table(kind, channels, tuple(domain), extrapolate)
    normalized = (np.asarray(value, np.float32) - np.float32(domain[0])) / np.float32(domain[1] - domain[0])
    if kind == "float_curve":
        mapped = lookup(table, float(normalized), extrapolate=extrapolate)
    else:
        mapped = np.asarray([lookup(table[:, i], float(normalized[i]), extrapolate=extrapolate) for i in range(3)])
    return np.asarray(value) + factor * (mapped - np.asarray(value))


def apply(node, channels, domain, extrapolate):
    node.parmTuple("curve_domain").set(domain)
    node.parm("curve_extension").set(0 if extrapolate else 1)
    for name, channel in zip(CHANNELS[node.type().name().split("::")[1]], channels):
        node.parm("curve_" + name).set(ramp(channel))


# Mixed bases, nonuniform keys, HDR and negative values, held ends.
CASES = {
    "float_mixed": dict(
        kind="float_curve",
        domain=(0.0, 1.0),
        channels=[(("linear", "catmull_rom", "monotone_cubic", "linear"), (0.0, 0.3, 0.6, 1.0), (0.0, 0.8, -0.4, 2.5))],
    ),
    "float_ranged": dict(
        kind="float_curve",
        domain=(-0.5, 2.0),
        channels=[(("constant", "linear", "bspline"), (0.1, 0.45, 1.0), (0.25, 1.5, 0.5))],
    ),
    "rgb_mixed": dict(
        kind="rgb_curves",
        domain=(0.0, 1.0),
        channels=[
            (("linear", "monotone_cubic", "linear"), (0.0, 0.5, 1.0), (0.0, 0.7, 1.0)),
            # A Constant step at the end of the ramp would make the reference depend
            # on float rounding of an input exactly at the step; keep it interior.
            (("linear", "constant", "linear"), (0.0, 0.4, 0.8), (0.1, 0.6, 1.2)),
            (("bezier", "bezier", "linear"), (0.0, 0.5, 1.0), (0.0, 0.2, 1.0)),
            (("hermite", "linear"), (0.0, 1.0), (1.0, 0.0)),
        ],
    ),
    "vector_mixed": dict(
        kind="vector_curves",
        domain=(-1.0, 1.0),
        channels=[
            (("linear", "catmull_rom", "linear"), (0.0, 0.5, 1.0), (-1.0, 0.3, 1.0)),
            (("linear", "linear"), (0.0, 1.0), (1.0, -1.0)),
            (("monotone_cubic", "monotone_cubic", "linear"), (0.0, 0.25, 1.0), (-2.0, 0.0, 3.0)),
        ],
    ),
}
