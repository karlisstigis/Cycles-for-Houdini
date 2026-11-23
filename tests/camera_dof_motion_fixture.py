# SPDX-License-Identifier: Apache-2.0
"""Continuous camera optics with an independent circular-pupil coverage integral.

The reference integrates authored equations, disk area and a box pixel filter.
It never calls the renderer's DOF interpolation or a captured sample array.
Polygon/panoramic optics have separate static-camera quadrature core controls.
"""

import pass_fixtures as pass_fixture

import json
import math

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, object_properties
from camera_lens_motion_fixture import Fixture as LensFixture


def plane(frame, name="combined"):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


class Fixture(LensFixture):
    width, height = 160, 96
    color = np.array((0.2, 0.4, 0.8))

    def __init__(self, device="CPU"):
        devices = json.loads(device) if isinstance(device, str) and device.startswith("[") else device
        super().__init__(devices[0] if isinstance(devices, list) else devices)
        device_selection.set_devices(self.rop, devices if isinstance(devices, list) else [devices])
        # The test needs an actually invalid source value. Houdini enforces a
        # strict minimum even for expressions; soften only this camera's local
        # spare template, before any session/viewer observes it. The slider
        # range and every production node/template remain unchanged.
        group = self.camera.parmTemplateGroup()
        ratio_template = group.find("cycles_aperture_ratio")
        ratio_template.setMinIsStrict(False)
        group.replace("cycles_aperture_ratio", ratio_template)
        self.camera.setParmTemplateGroup(group)
        self.grid.parmTuple("size").set((1.2, 40))
        pass_fixture.parm(self.camera, "shutter").set(0.5)
        self.camera.addSpareParmTuple(
            hou.FloatParmTemplate("test_dof_amplitude", "Test DOF Amplitude", 1, default_value=(1.0,))
        )
        pass_fixture.set_parameters(
            self.rop,
            dict(
                samples=1024,
                ipr_samples=1024,
                pixel_filter="box",
                filter_width=1,
                transparent_background=1,
                pass_position=0,
                pass_depth=0,
            ),
        )
        self.heavy = None
        self.set_case("focus")

    def set_case(self, case, *, offset=0, steps=3):
        # Only camera/ROP parameters change: the independently moving source SOP
        # remains untouched so camera-only capture retention is observable.
        self.case = case
        self.offset = offset
        self.steps = steps
        for name in (
            "focus",
            "fstop",
            "cycles_focus_distance",
            "cycles_fstop",
            "cycles_aperture_ratio",
            "cycles_aperture_rotation",
            "cycles_aperture_blades",
        ):
            pass_fixture.parm(self.camera, name).deleteAllKeyframes()
        pass_fixture.set_parameters(
            self.camera,
            dict(
                focus=2.5,
                fstop=0.05,
                cycles_focus_distance=2.5,
                cycles_fstop=0.05,
                cycles_aperture_ratio=1,
                cycles_aperture_rotation=0,
                cycles_aperture_blades=0,
                cycles_dof=1,
                projection=int(case == "ortho"),
                test_dof_amplitude=1,
                cycles_rolling_shutter="top" if case == "rolling" else "none",
                cycles_rolling_duration=0.4,
            ),
        )
        pass_fixture.set_parameters(
            self.rop,
            dict(
                allowmotionblur=int(case != "motion_off"),
                dof_mode="off" if case == "disabled" else "camera",
                shutteroffset=offset,
                motion_samples=steps,
                pass_motion=int(case == "motion_off"),
            ),
        )
        if case in (
            "focus",
            "ortho",
            "rolling",
            "blade_hold",
            "offframe_bad",
            "disabled",
            "motion_off",
            "nominal_bad",
            "ratio_bad",
        ):
            pass_fixture.parm(self.camera, "cycles_focus_distance").setExpression(
                '2.5+$T*96*ch("test_dof_amplitude")', hou.exprLanguage.Hscript
            )
        if case == "offframe_bad":
            pass_fixture.parm(self.camera, "cycles_focus_distance").setExpression(
                "float('nan') if hou.time()<0 else 2.5+hou.time()*96", hou.exprLanguage.Python
            )
        if case in ("nominal_bad", "disabled"):
            pass_fixture.parm(self.camera, "cycles_focus_distance").setExpression(
                "float('nan')", hou.exprLanguage.Python
            )
        if case == "ratio_bad":
            # Verify the softened fixture template really supplies an invalid
            # finite value; neither setter nor expression can silently clamp it.
            ratio = pass_fixture.parm(self.camera, "cycles_aperture_ratio")
            ratio.setExpression("1e-40", hou.exprLanguage.Python)
            assert 0 < ratio.eval() < 1e-38, ("Invalid ratio fixture was clamped", ratio.eval())
        if case == "aperture":
            pass_fixture.parm(self.camera, "cycles_fstop").setExpression(".025/(.5+$T*12)", hou.exprLanguage.Hscript)
        if case == "ratio":
            pass_fixture.parm(self.camera, "cycles_aperture_ratio").setExpression("1+$T*24", hou.exprLanguage.Hscript)
        if case == "rotation":
            pass_fixture.parm(self.camera, "cycles_aperture_rotation").setExpression(
                "$T*34560", hou.exprLanguage.Hscript
            )
        if case == "blade_hold":
            pass_fixture.parm(self.camera, "cycles_aperture_blades").setExpression(
                "if($T<0,5,0)", hou.exprLanguage.Hscript
            )

    def add_heavy(self):
        self.heavy = hou.node("/obj").createNode("geo", "dof_retained_instances", run_init_scripts=False)
        object_properties.install(self.heavy)
        pass_fixture.parm(self.heavy, "cycles_geometry_motion").set("velocity")
        self.prototype = self.heavy.createNode("torus", "million_triangle_prototype")
        pass_fixture.set_parameters(self.prototype, dict(type=0, rows=512, cols=1024))
        targets = self.heavy.createNode("grid", "twelve_targets")
        pass_fixture.set_parameters(targets, dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((18, 0, -8))
        copies = self.heavy.createNode("copytopoints", "shared_instances")
        copies.setInput(0, self.prototype)
        copies.setInput(1, targets)
        pass_fixture.parm(copies, "pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        pass_fixture.parm(self.rop, "vobject").set(self.obj.path() + " " + self.heavy.path())

    def capture_info(self, session):
        return ch._native.snapshot_info(session._controller.acknowledged)

    def expected_codes(self):
        return (
            {"camera.shutter_dof"}
            if self.case in ("offframe_bad", "blade_hold")
            else {"camera.invalid_dof"}
            if self.case in ("nominal_bad", "ratio_bad")
            else set()
        )

    def expected_samples(self):
        return (
            self.steps
            if self.case in ("focus", "ortho", "rolling", "blade_hold", "aperture", "ratio", "rotation")
            else 0
        )

    def reference(self):
        # 96-point exposure and 12-point spatial Gaussian quadrature. Uniform
        # exposure is exact identity-CDF in patch66; only the disk CDF is used.
        qt, qw = np.polynomial.legendre.leggauss(96)
        qt = (qt + 1) / 2
        qw = qw / 2
        px, pw = np.polynomial.legendre.leggauss(12)
        px = px / 2
        pw = pw / 2
        x = np.arange(self.width)[None, :, None] + 0.5 + px[None, None, :]
        y = np.arange(self.height)[:, None, None]
        rows = self.height if self.case == "rolling" else 1
        exposure = qt[:, None, None, None] * np.ones((1, rows, 1, 1))
        if self.case == "rolling":
            exposure = (exposure - 0.5) * 0.4 + (1 - y[None] / self.height - 0.5) * 0.6 + 0.5
        times = (exposure - 0.5 + self.offset / 2) * (0.5 / 24)
        blur = bool(pass_fixture.evaluate(self.rop, "allowmotionblur"))
        moving_times = times if blur else np.zeros_like(times)
        amplitude = pass_fixture.evaluate(self.camera, "test_dof_amplitude")
        focus = (
            2.5 + 96 * amplitude * times
            if self.case in ("focus", "ortho", "rolling", "blade_hold")
            else np.full_like(times, 2.5)
        )
        aperture = 0.5 + 12 * times if self.case == "aperture" else np.full_like(times, 0.5)
        ratio = 1 + 24 * times if self.case == "ratio" else np.ones_like(times)
        if self.case in ("nominal_bad", "disabled", "ratio_bad"):
            aperture = np.zeros_like(times)
        # Invalid off-frame focus holds requested DOF, independently of the
        # valid camera (+6t) and geometry (+2t) motion.
        if not blur:
            focus = np.full_like(times, 2.5)
            aperture = np.full_like(times, 0.5)
            ratio = np.ones_like(times)
        if self.case == "ortho":
            base = (2 * x / self.width - 1) * 3
        else:
            base = (2 * x / self.width - 1) * (10 / 3)
        base = base[None] + 4 * (hou.time() + moving_times)
        radius = np.abs(aperture / ratio * (1 - 5 / focus))

        def cdf(v):
            q = np.clip(v, -1, 1)
            return (q * np.sqrt(np.maximum(0, 1 - q * q)) + np.arcsin(q) + math.pi / 2) / math.pi

        safe = np.maximum(radius, 1e-100)
        coverage = cdf((0.6 - base) / safe) - cdf((-0.6 - base) / safe)
        coverage = np.where(radius < 1e-12, (np.abs(base) <= 0.6).astype(float), coverage)
        coverage = np.tensordot(qw, coverage, axes=(0, 0)) @ pw
        if rows == 1:
            coverage = np.broadcast_to(coverage, (self.height, self.width))
        return coverage[:, :, None] * self.color

    def inspect(self, frame, tolerance=0.0025):
        actual = plane(frame)[:, :, :3]
        expected = self.reference()
        delta = np.abs(actual - expected)
        result = dict(
            mean_difference=float(delta.mean()),
            maximum_difference=float(delta.max()),
            tolerance=tolerance,
            expected_integral="Authored lens equations × uniform exposure × circular disk marginal × box pixel footprint",
        )
        self.last_inspection = result
        assert np.isfinite(actual).all() and actual.max() > 0.1 and result["mean_difference"] < tolerance, result
        return result

    def update(self, camera_only=False):
        update = native_scene.capture(self.rop, camera_only=camera_only)
        update["passes"] = ["combined", "motion"] if self.case == "motion_off" else ["combined"]
        return update
