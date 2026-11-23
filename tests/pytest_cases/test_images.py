# SPDX-License-Identifier: Apache-2.0
"""Reference equations and adversarial controls for the image acceptance gate."""

import math

import pytest

from support.images import assert_image_close, rmse, ssim

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def image(height=13, width=15):
    return [[((y * 7 + x * 3) % 19) / 18 for x in range(width)] for y in range(height)]


def test_ssim_matches_independent_two_dimensional_reference():
    a = image()
    b = [[v * 0.8 + 0.05 for v in row] for row in a]
    weights = [(dy, dx, math.exp(-(dy * dy + dx * dx) / (2 * 1.5**2))) for dy in range(-5, 6) for dx in range(-5, 6)]
    norm = sum(w for _, _, w in weights)
    expected = []
    for y in range(5, len(a) - 5):
        for x in range(5, len(a[0]) - 5):
            samples = [(a[y + dy][x + dx], b[y + dy][x + dx], w / norm) for dy, dx, w in weights]
            ma = sum(v * w for v, _, w in samples)
            mb = sum(v * w for _, v, w in samples)
            va = sum(w * (v - ma) ** 2 for v, _, w in samples)
            vb = sum(w * (v - mb) ** 2 for _, v, w in samples)
            cov = sum(w * (u - ma) * (v - mb) for u, v, w in samples)
            expected.append(
                (2 * ma * mb + 0.0001) * (2 * cov + 0.0009) / ((ma**2 + mb**2 + 0.0001) * (va + vb + 0.0009))
            )
    assert ssim(a, b, data_range=1) == pytest.approx(sum(expected) / len(expected), abs=2e-14)


def test_ssim_constant_identity_scaling_and_channels():
    a = [[0.25] * 11 for _ in range(11)]
    b = [[0.5] * 11 for _ in range(11)]
    expected = (2 * 0.25 * 0.5 + 0.0001) / (0.25**2 + 0.5**2 + 0.0001)
    assert ssim(a, a, data_range=1) == pytest.approx(1)
    assert ssim(a, b, data_range=1) == pytest.approx(expected)
    assert ssim(
        [[v * 255 for v in row] for row in a], [[v * 255 for v in row] for row in b], data_range=255
    ) == pytest.approx(expected)
    rgb_a = [[[v, v, v] for v in row] for row in a]
    rgb_b = [[[v, v, 0.5] for v in row] for row in a]
    assert ssim(rgb_a, rgb_b, data_range=1, channel_axis=-1) == pytest.approx((2 + expected) / 3)
    with pytest.raises(ValueError, match="channel_axis"):
        ssim(rgb_a, rgb_b, data_range=1)


def test_local_structure_detects_equal_histogram_permutation_and_negative_scores():
    a = image(25, 25)
    b = [row[::-1] for row in a[::-1]]
    assert ssim(a, b, data_range=1) < 0.5
    anti = [[1 - v for v in row] for row in a]
    assert ssim(a, anti, data_range=1) < 0
    with pytest.raises(AssertionError, match="SSIM"):
        assert_image_close(a, b, tolerance=1, min_ssim=0.9, data_range=1)
    with pytest.raises(AssertionError, match="RMSE"):
        assert_image_close(a, b, tolerance=0.001, min_ssim=-1, data_range=1)
    assert assert_image_close(a, a, tolerance=0, min_ssim=1, data_range=1) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"data_range": 0},
        {"data_range": math.inf},
        {"data_range": None},
        {"data_range": 1, "window_size": 2},
        {"data_range": 1, "window_size": 1},
        {"data_range": 1, "sigma": 0},
        {"data_range": 1, "channel_axis": 0},
    ],
)
def test_ssim_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        ssim(image(), image(), **kwargs)


@pytest.mark.parametrize(
    "a,b",
    [
        ([], []),
        ([[0] * 11] * 10, [[0] * 11] * 10),
        ([[0], [0, 1]], [[0], [0, 1]]),
        ([[math.nan] * 11] * 11, [[0] * 11] * 11),
        (image(), image(14)),
        ([[0] * 11] * 11, [[math.inf] * 11] * 11),
    ],
)
def test_ssim_rejects_bad_shapes_and_nonfinite_data(a, b):
    with pytest.raises(ValueError):
        ssim(a, b, data_range=1)


def test_rmse_rejects_same_sample_count_different_shape():
    with pytest.raises(ValueError, match="shape"):
        rmse([[0, 1]], [[0], [1]])


def test_authored_large_ranges_are_not_inferred_or_overflowed():
    assert rmse([1e200, 0], [0, 0]) == pytest.approx(1e200 / math.sqrt(2))
    a = [[1e200] * 11 for _ in range(11)]
    b = [[5e199] * 11 for _ in range(11)]
    assert ssim(a, b, data_range=1e200) == pytest.approx(ssim([[1.0] * 11] * 11, [[0.5] * 11] * 11, data_range=1))
    with pytest.raises(ValueError, match="numeric range"):
        rmse([1e308], [-1e308])
    with pytest.raises(ValueError, match="real numbers"):
        rmse(["pixel"], [0])
