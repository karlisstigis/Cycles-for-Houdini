# SPDX-License-Identifier: Apache-2.0
"""Pure source-policy checks; no Houdini, native renderer or worker is loaded."""

from support.package_loader import load_module
import math
from pathlib import Path
import sys
import types
import unittest


root = Path(__file__).resolve().parents[1]
hou = types.ModuleType("hou")
hou.OperationFailed = type("OperationFailed", (Exception,), {})
sys.modules["hou"] = hou
package = types.ModuleType("cycles_houdini")
package.__path__ = [str(root / "houdini/python3.13libs/cycles_houdini")]
sys.modules["cycles_houdini"] = package
props = types.ModuleType("cycles_houdini.object_properties")
for name in ("install_tabs", "_section", "_toggle", "_float", "_integer", "_menu", "_condition"):
    setattr(props, name, lambda *a, **kw: None)
sys.modules[props.__name__] = props
camera = load_module(package, "camera_properties")


def lens(t=0, *, mode="camera", near=0.01, **edits):
    values = dict(
        cycles_dof=True,
        cycles_focus_distance=5.0,
        cycles_fstop=0.05,
        focal=50.0,
        focalunits="mm",
        cycles_aperture_ratio=1.0,
        cycles_aperture_rotation=0.0,
        cycles_aperture_blades=0,
    )
    values.update({k: (v(t) if callable(v) else v) for k, v in edits.items()})
    return camera.evaluate(lambda name, default: values.get(name, default), mode, unit_length=1, near_clip=near)


class CameraDofMotionSource(unittest.TestCase):
    def resolve(self, sample, now=0, times=(-1, 0, 1), near=0.01):
        return camera.resolve_lens(sample, now, times, "/obj/cam", near)

    def test_static_uses_requested_frame(self):
        r = self.resolve(lens)
        self.assertEqual(r["focus_distance"], 5)
        self.assertEqual(r["motion"], [])
        self.assertEqual(r["source_diagnostics"], [])

    def test_focus_pull_samples_all_shutter_keys(self):
        r = self.resolve(lambda t: lens(t, cycles_focus_distance=lambda t: 5 + 2 * t))
        self.assertEqual([s["focus_distance"] for s in r["motion"]], [3, 5, 7])
        self.assertEqual(r["focus_distance"], 5)

    def test_offset_does_not_replace_nominal_with_midpoint(self):
        r = self.resolve(lambda t: lens(t, cycles_focus_distance=lambda t: 5 + 2 * t), times=(1, 2, 3))
        self.assertEqual(r["focus_distance"], 5)
        self.assertEqual([s["focus_distance"] for s in r["motion"]], [7, 9, 11])

    def test_offframe_only_change_is_not_hidden_by_nominal(self):
        r = self.resolve(lambda t: lens(t, cycles_focus_distance=lambda t: 5 + t * t))
        self.assertEqual([s["focus_distance"] for s in r["motion"]], [6, 5, 6])

    def test_five_sample_curve_and_maximum_count(self):
        for count in (5, 129):
            times = [i / (count - 1) for i in range(count)]
            r = self.resolve(lambda t: lens(t, cycles_focus_distance=lambda t: 5 + t * t), times=times)
            self.assertEqual(len(r["motion"]), count)

    def test_all_four_continuous_fields_owned(self):
        r = self.resolve(
            lambda t: lens(
                t,
                cycles_focus_distance=lambda t: 5 + t,
                cycles_fstop=lambda t: 0.025 / (0.5 + 0.1 * t),
                cycles_aperture_ratio=lambda t: 1 + 0.2 * t,
                cycles_aperture_rotation=lambda t: 720 * t,
            )
        )
        self.assertEqual(set(r["motion"][0]), set(camera.DOF_CONTINUOUS))
        self.assertAlmostEqual(r["motion"][0]["aperture_size"], 0.4, places=6)
        self.assertAlmostEqual(r["motion"][2]["aperture_rotation"], 4 * math.pi, places=5)
        self.assertLess(r["motion"][0]["aperture_rotation"], -math.pi)

    def test_disabled_dof_does_not_read_bad_controls(self):
        for mode, enabled in (("off", True), ("camera", False)):
            r = self.resolve(
                lambda t: lens(
                    t,
                    mode=mode,
                    cycles_dof=enabled,
                    cycles_focus_distance=math.nan,
                    cycles_fstop=math.nan,
                    cycles_aperture_ratio=math.nan,
                )
            )
            self.assertEqual(r["aperture_size"], 0)
            self.assertEqual(r["motion"], [])
            self.assertFalse(r["source_diagnostics"])

    def test_global_on_uses_camera_optics(self):
        r = self.resolve(lambda t: lens(t, mode="on", cycles_dof=False, cycles_focus_distance=lambda t: 5 + t))
        self.assertEqual(len(r["motion"]), 3)

    def test_offframe_nonfinite_holds_nominal(self):
        r = self.resolve(lambda t: lens(t, cycles_focus_distance=math.nan if t < 0 else 5))
        self.assertEqual(r["focus_distance"], 5)
        self.assertEqual(r["motion"], [])
        self.assertEqual(r["source_diagnostics"][0]["code"], "camera.shutter_dof")

    def test_invalid_nominal_uses_pinhole(self):
        r = self.resolve(lambda t: lens(t, cycles_focus_distance=math.nan))
        self.assertEqual(r["aperture_size"], 0)
        self.assertEqual(r["motion"], [])
        self.assertEqual(r["source_diagnostics"][0]["code"], "camera.invalid_dof")

    def test_repair_returns_to_identical_accepted_state(self):
        good = lambda t: lens(t, cycles_focus_distance=lambda t: 5 + t)
        before = self.resolve(good)
        broken = self.resolve(lambda t: lens(t, cycles_focus_distance=math.nan if t < 0 else 5))
        self.assertTrue(broken["source_diagnostics"])
        self.assertEqual(self.resolve(good), before)

    def test_discrete_blades_hold_without_losing_focus_pull(self):
        r = self.resolve(
            lambda t: lens(t, cycles_focus_distance=lambda t: 5 + t, cycles_aperture_blades=3 if t <= 0 else 5)
        )
        self.assertEqual(r["aperture_blades"], 3)
        self.assertEqual(len(r["motion"]), 3)
        self.assertEqual(r["source_diagnostics"][0]["attribute"], "aperture_blades")

    def test_bad_discrete_shutter_input_holds_usable_optics(self):
        r = self.resolve(lambda t: lens(t, cycles_aperture_blades=math.inf if t < 0 else 0))
        self.assertEqual(r["motion"], [])
        self.assertEqual(r["source_diagnostics"][0]["code"], "camera.shutter_dof")

    def test_inverse_ratio_overflow_is_local(self):
        r = self.resolve(lambda t: lens(t, cycles_aperture_ratio=1e-40))
        self.assertEqual(r["aperture_size"], 0)
        self.assertEqual(r["source_diagnostics"][0]["code"], "camera.invalid_dof")

    def test_aperture_divided_by_ratio_overflow_is_local(self):
        r = self.resolve(lambda t: lens(t, cycles_fstop=2.5e-32, cycles_aperture_ratio=1e-20))
        self.assertEqual(r["aperture_size"], 0)
        self.assertTrue(r["source_diagnostics"])

    def test_near_focus_origin_bound_overflow_is_local(self):
        r = self.resolve(lambda t: lens(t, near=1e30, cycles_fstop=2.5e-22, cycles_focus_distance=1e-5), near=1e30)
        self.assertEqual(r["aperture_size"], 0)
        self.assertTrue(r["source_diagnostics"])

    def test_combined_extrema_must_be_safely_bounded(self):
        def sample(t):
            if t < 0:
                return lens(t, cycles_fstop=2.5e-27, cycles_aperture_ratio=1e25)
            if t > 0:
                return lens(t, cycles_fstop=0.025, cycles_aperture_ratio=1e-25)
            return lens(t)

        r = self.resolve(sample)
        self.assertEqual(r["aperture_size"], 0.5)
        self.assertEqual(r["motion"], [])
        self.assertEqual(r["source_diagnostics"][0]["code"], "camera.shutter_dof")

    def test_large_finite_focus_is_not_a_source_failure(self):
        r = self.resolve(lambda t: lens(t, cycles_focus_distance=1e30))
        self.assertFalse(r["source_diagnostics"])
        self.assertGreater(r["focus_distance"], 1e29)

    def test_invalid_internal_sample_count_stays_strict(self):
        with self.assertRaisesRegex(ValueError, "odd 3..129"):
            self.resolve(lambda t: lens(t, cycles_focus_distance=lambda t: 5 + t), times=(0, 1))


if __name__ == "__main__":
    unittest.main()
