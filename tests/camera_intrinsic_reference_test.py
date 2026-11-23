# SPDX-License-Identifier: Apache-2.0
"""SDK-free analytic controls for the independently authored ray reference."""

from support.paths import artifacts_root
import argparse, json, sys
from pathlib import Path
import unittest
import numpy as np
from camera_intrinsic_reference import authored, directions, exposure_reference, COLORS, DURATION


class CameraIntrinsicReference(unittest.TestCase):
    def test_equirectangular_axes(self):
        spec = authored("equirectangular", 0)
        spec.update(latitude_min=-90, latitude_max=90)
        u = np.array([0, 0.25, 0.5, 0.75])
        v = np.full_like(u, 0.5)
        d, valid = directions(spec, u, v, 2, 1)
        np.testing.assert_allclose(d, [[0, 0, 1], [-1, 0, 0], [0, 0, -1], [1, 0, 0]], atol=1e-14)
        self.assertTrue(valid.all())

    def test_equisolid_focal_sensor_similarity(self):
        spec = authored("fisheye_equisolid", 0)
        u = np.array([0.4, 0.5, 0.6])
        v = np.full_like(u, 0.5)
        a, va = directions(spec, u, v, 2, 1)
        spec.update(sensor_width=72, lens=21)
        b, vb = directions(spec, u, v, 2, 1)
        np.testing.assert_array_equal(va, vb)
        np.testing.assert_allclose(a, b, atol=1e-14)

    def test_vertical_fit_ignores_width(self):
        spec = authored("sensor", 0)
        u = np.array([0.4, 0.5, 0.6])
        v = np.full_like(u, 0.5)
        a, va = directions(spec, u, v, 2, 1)
        spec["sensor_width"] = float("nan")
        b, vb = directions(spec, u, v, 2, 1)
        np.testing.assert_array_equal(a, b)
        np.testing.assert_array_equal(va, vb)

    def test_extra_keys_have_nominal_and_endpoint_negative_control(self):
        values = [authored("extra_fov", DURATION * (t - 0.5))["tangent"] for t in (0, 0.25, 0.5, 0.75, 1)]
        np.testing.assert_allclose(values, [0.6, 0.45, 0.6, 0.75, 0.6], atol=1e-14)
        self.assertEqual(authored("extra_fov", 0, 0.5)["tangent"], 0.6)
        self.assertNotEqual(authored("extra_fov", DURATION / 4, 0.5)["tangent"], values[3])

    def test_changing_fov_is_visible_against_centre_hold(self):
        actual = exposure_reference("perspective", 32, 16, time_samples=24, spatial=2)
        held = exposure_reference("perspective", 32, 16, blur=False, spatial=2)
        self.assertGreater(float(np.abs(actual - held).mean()), 0.001)

    def test_near_clipping_has_exact_half_exposure(self):
        actual = exposure_reference("clipping", 3, 3, time_samples=64, spatial=1)
        np.testing.assert_allclose(actual[1, 1], COLORS[5] * 0.5, atol=1e-7)

    def test_far_clipping_has_exact_half_exposure(self):
        actual = exposure_reference("ortho_clip", 3, 3, time_samples=64, spatial=1)
        np.testing.assert_allclose(actual[1, 1], COLORS[5] * 0.5, atol=1e-7)

    def test_orthographic_outside_origins_do_not_hit_inside_walls(self):
        actual = exposure_reference("orthographic", 8, 4, blur=False, spatial=1)
        np.testing.assert_array_equal(actual[:, 0], 0)
        np.testing.assert_array_equal(actual[:, -1], 0)
        np.testing.assert_allclose(actual[2, 4], COLORS[5], atol=1e-7)

    def test_inactive_panoramic_controls_are_not_consumed(self):
        spec = authored("equirectangular", 0)
        u = np.array([0.3, 0.6])
        v = np.full_like(u, 0.5)
        a, valid = directions(spec, u, v, 2, 1)
        spec.update(lens=float("nan"), sensor_width=float("nan"), coefficients=(float("nan"),) * 5)
        b, again = directions(spec, u, v, 2, 1)
        np.testing.assert_array_equal(a, b)
        np.testing.assert_array_equal(valid, again)

    def test_shifted_shutter_changes_lens_without_replacing_nominal(self):
        nominal = authored("equirectangular", 0)
        actual = exposure_reference("equirectangular", 24, 12, offset=1, time_samples=24, spatial=1)
        centred = exposure_reference("equirectangular", 24, 12, time_samples=24, spatial=1)
        self.assertGreater(float(np.abs(actual - centred).mean()), 0.003)
        self.assertEqual(nominal, authored("equirectangular", 0))

    def test_radius_authorship_has_independent_affine_slope(self):
        for t in (-DURATION / 2, 0, DURATION / 2):
            spec = authored("cylinder_radius", t)
            self.assertAlmostEqual(spec["v_min"] / spec["radius"], -(1 + 12 * t))
            self.assertAlmostEqual(spec["v_max"] / spec["radius"], 1 + 12 * t)

    def test_supported_models_produce_finite_independent_exposures(self):
        for model in (
            "perspective",
            "orthographic",
            "equirectangular",
            "cylinder",
            "cylinder_radius",
            "polynomial",
            "fisheye_equidistant",
            "fisheye_equisolid",
            "sensor",
        ):
            with self.subTest(model=model):
                image = exposure_reference(model, 12, 6, time_samples=8, spatial=1)
                self.assertEqual(image.dtype, np.float32)
                self.assertTrue(np.isfinite(image).all())
                self.assertGreater(image.max(), 0.1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--report", type=Path, default=artifacts_root() / "stage2/cameras/intrinsic-motion/reference-unit.json"
    )
    args = p.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CameraIntrinsicReference)
    )
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
