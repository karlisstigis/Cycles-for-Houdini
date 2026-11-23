# SPDX-License-Identifier: Apache-2.0
"""Matched-seed regional bias statistics; the core needs only Python's stdlib."""

import math
from itertools import product


BIAS_SEEDS = (701, 1103, 1409, 1901, 2203, 2707, 3203, 3701)
ABSOLUTE_BOUND = 0.01
NOISE_CEILING = 0.01
PERMUTATION_ALPHA = 0.01


def regions(image):
    """Extract the 4 x 4 spatial grid and RGB means from one NumPy image."""
    return [image[y : y + 16, x : x + 24].mean((0, 1)).tolist() for y in range(0, 64, 16) for x in range(0, 96, 24)]


def evaluate_regions(reference, candidate, *, bit_identical=False, feature=False):
    """Enumerate every within-seed label swap of the max signed regional mean.

    This is exact under the paired *symmetric difference* null. Distinct
    estimators can have the same expectation without that symmetry; high SPP
    and the separate noise guard help, but cannot prove the assumption.
    """
    if len(reference) != len(candidate) or len(reference) != len(BIAS_SEEDS):
        raise ValueError("Bias comparison requires eight matched seed pairs")
    if any(
        len(image) != 16 or any(len(rgb) != 3 or not all(math.isfinite(value) for value in rgb) for rgb in image)
        for image in (*reference, *candidate)
    ):
        raise ValueError("Bias comparison requires finite 16-region RGB arrays")
    if bit_identical:
        return dict(
            result="inconclusive" if feature else "bit-identical",
            bit_identical_all_seeds=True,
            max_signed_regional_rgb=0.0,
            max_paired_standard_error=0.0,
            permutation_p=1.0,
            permutation_count=256,
            signed_regional_rgb=[[0.0] * 3 for _ in range(16)],
            worst_region=dict(row=0, column=0, channel="R", signed_effect=0.0),
        )
    difference = [[float(b[r][c] - a[r][c]) for r in range(16) for c in range(3)] for a, b in zip(reference, candidate)]
    n = len(difference)
    signed = [sum(pair[k] for pair in difference) / n for k in range(48)]
    worst = max(range(48), key=lambda k: abs(signed[k]))
    observed = abs(signed[worst])
    noise = max(math.sqrt(sum((pair[k] - signed[k]) ** 2 for pair in difference) / (n - 1) / n) for k in range(48))
    exceed = 0
    for signs in product((-1, 1), repeat=n):
        statistic = max(abs(sum(signs[i] * difference[i][k] for i in range(n)) / n) for k in range(48))
        exceed += statistic >= observed - 1e-12
    p = exceed / (1 << n)
    state = (
        "inconclusive"
        if noise > NOISE_CEILING
        else "failed"
        if observed > ABSOLUTE_BOUND or p <= PERMUTATION_ALPHA
        else "passed"
    )
    return dict(
        result=state,
        bit_identical_all_seeds=False,
        max_signed_regional_rgb=observed,
        max_paired_standard_error=noise,
        permutation_p=p,
        permutation_count=1 << n,
        signed_regional_rgb=[signed[r * 3 : (r + 1) * 3] for r in range(16)],
        worst_region=dict(
            row=worst // 12,
            column=(worst // 3) % 4,
            channel="RGB"[worst % 3],
            signed_effect=signed[worst],
        ),
    )


def evaluate(reference, candidate, *, feature=False):
    """Validate finite full images and retain a full-image bit-identity verdict."""
    import numpy as np

    if len(reference) != len(candidate) or len(reference) != len(BIAS_SEEDS):
        raise ValueError("Bias comparison requires eight matched seed pairs")
    if any(
        image.shape != (64, 96, 3) or not np.issubdtype(image.dtype, np.number) or not np.isfinite(image).all()
        for image in (*reference, *candidate)
    ):
        raise ValueError("Bias comparison requires finite 64 x 96 linear RGB arrays")
    return evaluate_regions(
        [regions(image) for image in reference],
        [regions(image) for image in candidate],
        bit_identical=all(np.array_equal(a, b) for a, b in zip(reference, candidate)),
        feature=feature,
    )
