# SPDX-License-Identifier: Apache-2.0
"""Nominal-frame aperture policy, unsupported builds and no host-thread image I/O."""

from support.package_loader import load_module
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("_iris_controls_source")
package.__path__ = [str(ROOT / "houdini/python3.13libs/cycles_houdini")]
sys.modules[package.__name__] = package
native = types.ModuleType(package.__name__ + "._native")
native.capabilities = lambda: dict(image_iris=True)
sys.modules[native.__name__] = native
package._native = native
hou = types.ModuleType("hou")
hou.getenv = lambda name: str(ROOT) if name == "HIP" else None
hou.OperationFailed = type("OperationFailed", (Exception,), {})
sys.modules["hou"] = hou
props = types.ModuleType(package.__name__ + ".object_properties")
for name in ("install_tabs", "_section", "_toggle", "_float", "_integer", "_menu", "_condition"):
    setattr(props, name, lambda *args, **kw: None)
sys.modules[props.__name__] = props
camera = load_module(package, "camera_properties")
iris = load_module(package, "image_iris")


def sample(t, **edits):
    values = dict(
        cycles_dof=True,
        cycles_focus_distance=5.0,
        cycles_fstop=0.05,
        focal=50.0,
        focalunits="mm",
        cycles_image_iris=True,
        cycles_image_iris_file="masks/aperture.exr",
        cycles_image_iris_method="hierarchical",
    )
    values.update({k: (v(t) if callable(v) else v) for k, v in edits.items()})
    return camera.evaluate(lambda name, default: values.get(name, default), unit_length=1)


class ImageIrisControlsSource(unittest.TestCase):
    def setUp(self):
        iris.supported.cache_clear()

    def tearDown(self):
        iris.supported.cache_clear()

    def resolve(self, **edits):
        return camera.resolve_lens(lambda t: sample(t, **edits), 0, [-1, 0, 1], "/obj/camera")

    def test_relative_path_uses_hip_without_touching_file(self):
        with patch("builtins.open", side_effect=AssertionError("Camera source must not decode an image")):
            result = self.resolve()
        self.assertEqual(result["image_iris"], os.path.normpath(str(ROOT / "masks/aperture.exr")))
        self.assertEqual(result["image_iris_method"], 2)
        self.assertNotIn("_image_iris_source", result)

    def test_cdf_is_distinct_and_off_keeps_analytic_path(self):
        self.assertEqual(self.resolve(cycles_image_iris_method="cdf")["image_iris_method"], 1)
        self.assertFalse(self.resolve()["image_iris_color"])
        result = self.resolve(cycles_image_iris=False)
        self.assertEqual((result["image_iris"], result["image_iris_method"]), ("", 0))
        self.assertFalse(result["image_iris_color"])

    def test_color_is_optional_and_held_at_requested_frame(self):
        result = self.resolve(cycles_image_iris_color=lambda t: t == 0)
        self.assertTrue(result["image_iris_color"])
        self.assertTrue(all("image_iris_color" not in value for value in result["motion"]))
        self.assertFalse(self.resolve(cycles_image_iris=False, cycles_image_iris_color=True)["image_iris_color"])
        self.assertFalse(self.resolve(cycles_dof=False, cycles_image_iris_color=True)["image_iris_color"])

    def test_image_and_method_held_at_requested_frame(self):
        result = self.resolve(
            cycles_image_iris_file=lambda t: "masks/aperture.exr" if t == 0 else "",
            cycles_image_iris_method=lambda t: "hierarchical" if t == 0 else "unknown",
            cycles_focus_distance=lambda t: 5 + t,
        )
        self.assertEqual(result["image_iris_method"], 2)
        self.assertEqual([v["focus_distance"] for v in result["motion"]], [4, 5, 6])

    def test_inactive_dof_ignores_invalid_image_and_method(self):
        result = self.resolve(cycles_dof=False, cycles_image_iris_file="", cycles_image_iris_method="unknown")
        self.assertEqual((result["image_iris"], result["image_iris_method"]), ("", 0))

    def test_unsupported_enabled_build_rejects_without_silent_circle(self):
        with patch.object(native, "capabilities", return_value={"image_iris": False}):
            with self.assertRaisesRegex(ValueError, "does not include"):
                self.resolve()
            self.assertEqual(self.resolve(cycles_dof=False)["image_iris_method"], 0)

    def test_image_iris_ignores_unused_blades_without_disabling_dof(self):
        baseline = self.resolve(cycles_aperture_blades=0)
        for blades in (1, 2, 3, 32, float("nan"), "unused"):
            self.assertEqual(self.resolve(cycles_aperture_blades=blades), baseline)
        self.assertGreater(baseline["aperture_size"], 0)
        self.assertEqual(baseline["aperture_blades"], 0)
        self.assertEqual(baseline["source_diagnostics"], [])
        invalid = self.resolve(cycles_image_iris=False, cycles_aperture_blades=2)
        self.assertEqual(invalid["aperture_size"], 0)
        self.assertEqual(invalid["source_diagnostics"][0]["attribute"], "aperture_blades")

    def test_bad_nominal_image_and_unknown_method_remain_visible_errors(self):
        with self.assertRaisesRegex(ValueError, "requires an image file"):
            self.resolve(cycles_image_iris_file="")
        with self.assertRaisesRegex(ValueError, "Unknown Image Iris"):
            self.resolve(cycles_image_iris_method="unknown")


if __name__ == "__main__":
    unittest.main()
