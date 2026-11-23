# SPDX-License-Identifier: Apache-2.0
"""Pure numerical contracts for the matched-seed regional bias rule."""

import math
from pathlib import Path
import random
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from heavy_bias_statistics import BIAS_SEEDS, evaluate_regions  # noqa: E402

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def constant(value=0.3):
    return [[value] * 3 for _ in range(16)]


def test_bit_identical_defaults_and_inactive_feature():
    images = [constant() for _ in BIAS_SEEDS]
    assert evaluate_regions(images, images, bit_identical=True)["result"] == "bit-identical"
    assert evaluate_regions(images, images, bit_identical=True, feature=True)["result"] == "inconclusive"


def test_noisy_matched_null_is_not_a_false_failure():
    rng = random.Random(151)
    false_failures = 0
    for _ in range(120):
        reference = [[[0.3 + rng.gauss(0, 0.005) for _ in range(3)] for _ in range(16)] for _ in BIAS_SEEDS]
        candidate = [[[0.3 + rng.gauss(0, 0.005) for _ in range(3)] for _ in range(16)] for _ in BIAS_SEEDS]
        false_failures += evaluate_regions(reference, candidate)["result"] == "failed"
    print(f"seed=151 null_trials=120 false_failures={false_failures}")
    assert false_failures <= 4


def test_injected_positive_and_negative_local_energy_bias_is_detected():
    reference = [constant() for _ in BIAS_SEEDS]
    for sign in (-1, 1):
        candidate = [constant() for _ in BIAS_SEEDS]
        for region in candidate:
            region[5][0] += sign * 0.006
        report = evaluate_regions(reference, candidate, feature=True)
        assert report["result"] == "failed"
        assert report["permutation_p"] == 2 / 256
        assert report["max_signed_regional_rgb"] < 0.01
        assert (report["worst_region"]["row"], report["worst_region"]["column"], report["worst_region"]["channel"]) == (
            1,
            1,
            "R",
        )
        assert math.copysign(1, report["worst_region"]["signed_effect"]) == sign


def test_seed_disagreement_and_nonfinite_input():
    reference = [constant() for _ in BIAS_SEEDS]
    candidate = [constant() for _ in BIAS_SEEDS]
    for i, region in enumerate(candidate):
        region[5][0] += 0.03 if i % 2 else -0.03
    assert evaluate_regions(reference, candidate)["result"] == "inconclusive"
    candidate[0][5][0] = math.nan
    with pytest.raises(ValueError, match="finite"):
        evaluate_regions(reference, candidate)
