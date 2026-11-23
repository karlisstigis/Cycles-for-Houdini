# SPDX-License-Identifier: Apache-2.0
"""Finite, shape-aware RMSE and local structural image comparisons.

SSIM follows Wang et al. (2004), Eq. 13, using population moments and a
normalized Gaussian window (11x11, sigma=1.5 by default). Only complete windows
are pooled: no padded border pixels. Channels are scored independently and
averaged equally; callers must explicitly select RGB/alpha and color space.
https://ece.uwaterloo.ca/~z70wang/publications/ssim.pdf
"""

import math
from numbers import Real


def _samples(value):
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, Real):
        if not math.isfinite(value):
            raise ValueError("Image samples must be finite")
        return (), [float(value)]
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Image samples must be real numbers")
    try:
        children = [_samples(item) for item in value]
    except TypeError as error:
        raise ValueError("Image samples must be real numbers") from error
    if not children or any(shape != children[0][0] for shape, _ in children):
        raise ValueError("Images must be nonempty rectangular arrays")
    return (len(children),) + children[0][0], [x for _, row in children for x in row]


def _pair(actual, expected):
    shape, a = _samples(actual)
    other, b = _samples(expected)
    if not shape or shape != other:
        raise ValueError("Images must have the same nonzero shape and sample count")
    return shape, a, b


def rmse(actual, expected):
    _, actual, expected = _pair(actual, expected)
    differences = [a - b for a, b in zip(actual, expected)]
    scale = max(abs(value) for value in differences)
    if not math.isfinite(scale):
        raise ValueError("Image differences exceed the finite numeric range")
    if scale == 0:
        return 0.0
    return scale * math.sqrt(math.fsum((value / scale) ** 2 for value in differences) / len(differences))


def ssim(actual, expected, *, data_range, channel_axis=None, window_size=11, sigma=1.5):
    """Mean valid-window SSIM for HxW or explicitly channel-last HxWxC data.

    ``data_range`` is the authored intensity span (1 for normalized scene-linear
    RGB, 255 for 8-bit values), never inferred from a particular image. Samples
    are neither clipped nor rescaled. HDR callers must choose a meaningful fixed
    span. No transfer function, luminance conversion, or alpha removal is implicit.
    Negative scores are retained (opposite structures can be anticorrelated).
    """
    if not isinstance(data_range, Real) or not math.isfinite(data_range) or data_range <= 0:
        raise ValueError("data_range must be finite and positive")
    if isinstance(window_size, bool) or not isinstance(window_size, int) or window_size < 3 or window_size % 2 != 1:
        raise ValueError("window_size must be an odd integer at least 3")
    if not isinstance(sigma, Real) or not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("sigma must be finite and positive")
    shape, a, b = _pair(actual, expected)
    if channel_axis is None and len(shape) == 2:
        height, width = shape
        channels = 1
    elif channel_axis in (-1, 2) and len(shape) == 3:
        height, width, channels = shape
    else:
        raise ValueError("SSIM requires HxW with channel_axis=None or HxWxC with channel_axis=-1")
    if min(height, width) < window_size:
        raise ValueError("Image spatial dimensions must be at least window_size")
    half = window_size // 2
    weights = [math.exp(-0.5 * (offset / sigma) ** 2) for offset in range(-half, half + 1)]
    total = math.fsum(weights)
    weights = [weight / total for weight in weights]
    out_width = width - window_size + 1

    def blur(values):
        rows = [
            math.fsum(weights[k] * values[(y * width + x + k) * channels + c] for k in range(window_size))
            for y in range(height)
            for x in range(out_width)
            for c in range(channels)
        ]
        return [
            math.fsum(weights[k] * rows[((y + k) * out_width + x) * channels + c] for k in range(window_size))
            for y in range(height - window_size + 1)
            for x in range(out_width)
            for c in range(channels)
        ]

    # Normalize first, preventing overflow for ordinary high-range integer data.
    a, b = [x / data_range for x in a], [x / data_range for x in b]
    ma, mb = blur(a), blur(b)
    aa, bb, ab = blur([x * x for x in a]), blur([x * x for x in b]), blur([x * y for x, y in zip(a, b)])
    scores = []
    for x, y, xx, yy, xy in zip(ma, mb, aa, bb, ab):
        va, vb = max(0.0, xx - x * x), max(0.0, yy - y * y)
        bound = math.sqrt(va * vb)
        covariance = max(-bound, min(bound, xy - x * y))
        scores.append(
            ((2 * x * y + 0.01**2) * (2 * covariance + 0.03**2)) / ((x * x + y * y + 0.01**2) * (va + vb + 0.03**2))
        )
    if not all(math.isfinite(score) for score in scores):
        raise ValueError("Image range cannot be represented safely")
    return math.fsum(scores) / len(scores)


def assert_image_close(
    actual, expected, *, tolerance, min_ssim=None, data_range=None, channel_axis=None, window_size=11, sigma=1.5
):
    """Require RMSE, and optionally independent local structure, within limits."""
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Tolerance must be finite and nonnegative")
    if min_ssim is not None and (not math.isfinite(min_ssim) or not -1 <= min_ssim <= 1):
        raise ValueError("min_ssim must be finite and in [-1, 1]")
    error = rmse(actual, expected)
    score = (
        None
        if min_ssim is None
        else ssim(
            actual, expected, data_range=data_range, channel_axis=channel_axis, window_size=window_size, sigma=sigma
        )
    )
    assert error <= tolerance, f"Image RMSE {error:.8g} exceeds {tolerance:.8g}"
    if score is not None:
        assert score >= min_ssim, f"Image SSIM {score:.8g} is below {min_ssim:.8g}"
    return error
