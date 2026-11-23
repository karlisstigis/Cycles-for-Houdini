# SPDX-License-Identifier: Apache-2.0
"""Independent analytic checks for finite frustum numeric/image test references."""

from support.paths import artifacts_root
import argparse
import json
from pathlib import Path
import sys
import unittest
import numpy as np
import tapered_volume_reference as r


class ReferenceTests(unittest.TestCase):
    def field(self, taper=(1, 1), n=13):
        return dict(
            name="density",
            resolution=[n, n, n],
            taper=list(taper),
            matrix=np.eye(4).ravel().tolist(),
            centres=[dict(index=[0, 0, 0], position=r.forward(np.full(3, 0.5 / n), taper).tolist())],
        )

    def test_voxel_centres_use_half_index(self):
        self.assertEqual(r.assert_centres(self.field((2, 0.4))), 0)

    def test_positive_plan_retains_narrow_spacing(self):
        extent, target, ok = r.target_plan((13, 11, 29), (0.5, 1.8))
        self.assertTrue(ok)
        self.assertEqual(target.tolist(), [52, 40, 58])
        np.testing.assert_array_less(2 * extent / target, np.array((0.5 / 13, 1 / 11, 1 / 29)) + 1e-15)

    def test_budget_is_explicit_not_coarsened(self):
        _, target, ok = r.target_plan((128, 128, 128), (1.2, 1.2))
        self.assertTrue(ok)
        _, target, ok = r.target_plan((512, 512, 512), (2, 2))
        self.assertFalse(ok)
        _, target, ok = r.target_plan((13, 11, 29), (1e-8, 1))
        self.assertFalse(ok)
        self.assertGreater(target[0], 2048)

    def test_oriented_lattice_does_not_use_world_aabb(self):
        a = r.target_plan((13, 11, 29), (2, 1.5))
        self.assertEqual(a[1].tolist(), [52, 33, 58])

    def test_constant_integer_support_not_streaked_outside(self):
        f = self.field((2, 1.5))
        f["name"] = "integer_auxiliary"
        result = r.numerical(f)
        self.assertLess(result["active_voxels"], np.prod(result["target"]))
        self.assertEqual(result["active_sums"], [result["active_voxels"], 0, 0])

    def test_collapsed_apex_converges_to_true_cone_volume(self):
        errors = []
        for n in (13, 26):
            f = self.field((0, 0), n)
            f["name"] = "integer_auxiliary"
            v = r.numerical(f)
            estimate = v["active_sums"][0] * (1 / n) ** 3
            errors.append(abs(estimate - 8 / 3))
        self.assertLess(errors[1], errors[0] * 0.35)

    def test_signed_hourglass_ray_has_two_finite_sections(self):
        origin = np.array([[[0.0, 0.0, 5.0]]])
        direction = np.array([0.0, 0.0, -1.0])
        intervals = r._segments(origin, direction, np.array((-1.0, -1.0)))
        self.assertEqual(len(intervals), 2)
        self.assertAlmostEqual(sum(float((b - a).item()) for a, b in intervals), 2)

    def test_slab_beer_lambert_reference(self):
        image = r.image(self.field(), width=12, height=8, level=0.35, integer=True, spp=1)
        np.testing.assert_allclose(image[4, 6], np.full(3, np.exp(-0.42 * 2)), atol=1e-7)
        np.testing.assert_array_equal(image[0, 0], np.ones(3))

    def test_empty_field_has_no_extinction(self):
        image = r.image(self.field((0, 0)), width=12, height=8, level=0, spp=1)
        np.testing.assert_array_equal(image, np.ones_like(image))

    def test_native_float_streak_interpolation_reference(self):
        u = np.array((0.01, 0.5, 0.99))
        v = r.values(u, (5, 4, 3), "density", 0.35)
        self.assertAlmostEqual(v, 0.35 * (1 + 0.2 * 0.1 + 0.1 * 0.5 + 0.15 * (1 - 1 / 6)))

    def test_vec3_channels_are_interpolated_independently(self):
        np.testing.assert_allclose(
            r.values(np.array((0.3, 0.4, 0.5)), (5, 4, 3), "tint"), (0.216, 0.392, 0.67), atol=1e-12
        )

    def test_constant_border_blends_zero_inside_outer_half_voxel(self):
        unit = np.array((0.01, 0.25, 0.99))
        res = np.array((13, 11, 29))
        weight = (0.01 * 13 + 0.5) * (0.01 * 29 + 0.5)
        streak = r.values(unit, res, "density", 0.35)
        self.assertAlmostEqual(r.values(unit, res, "density", 0.35, 0), streak * weight)

    def test_native_integer_constant_border_truncates_partial_blend(self):
        self.assertEqual(r.values(np.array((0.01, 0.25, 0.99)), (13, 11, 29), "integer_density", 0.35, 0), 0)
        self.assertEqual(r.values(np.array((0.3, 0.4, 0.5)), (13, 11, 29), "integer_density", 0.35, 0), 1)

    def test_reflection_preserves_finite_optical_depth(self):
        f = self.field()
        a = r.image(f, width=12, height=8, integer=True, spp=1)
        m = np.eye(4)
        m[0, 0] = -1
        f["matrix"] = m.ravel().tolist()
        np.testing.assert_array_equal(a, r.image(f, width=12, height=8, integer=True, spp=1))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path, default=artifacts_root() / "stage3/volumes/tapered/reference-unit.json")
    args, remaining = p.parse_known_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReferenceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            dict(
                result="passed" if result.wasSuccessful() else "failed",
                tests=result.testsRun,
                failures=[str(e) for _, e in result.failures],
                errors=[str(e) for _, e in result.errors],
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
