# SPDX-License-Identifier: Apache-2.0
"""Actual Houdini camera controls and independently moving colored enclosure."""

import pass_fixtures as pass_fixture

import json
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, object_properties
from panorama_fixture import Fixture as PanoramaFixture
from camera_intrinsic_reference import exposure_reference, DURATION


def plane(frame, name="combined"):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


class Fixture(PanoramaFixture):
    width, height = 128, 64

    def __init__(self, device="CPU"):
        devices = json.loads(device) if isinstance(device, str) and device.startswith("[") else device
        super().__init__(devices[0] if isinstance(devices, list) else devices)
        device_selection.set_devices(self.rop, devices if isinstance(devices, list) else [devices])
        self.parent = hou.node("/obj").createNode("null", "intrinsic_independent_motion")
        pass_fixture.parm(self.parent, "tx").setExpression("$T*.1", hou.exprLanguage.Hscript)
        self.outputs = []
        for index, (obj, sop) in enumerate(zip(self.objects, self.sops)):
            obj.setInput(0, self.parent)
            output = obj.createNode("attribwrangle", "independent_geometry_motion")
            output.setInput(0, sop)
            pass_fixture.parm(output, "snippet").set("@P." + ("z" if index < 2 else "x") + "+=@Time*2;")
            output.setDisplayFlag(True)
            output.setRenderFlag(True)
            self.outputs.append(output)
        self.camera.addSpareParmTuple(
            hou.FloatParmTemplate("test_intrinsic_amplitude", "Test Intrinsic Amplitude", 1, default_value=(1.0,))
        )
        pass_fixture.parm(self.camera, "tx").setExpression("$T*6", hou.exprLanguage.Hscript)
        pass_fixture.parm(self.camera, "shutter").set(0.5)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                samples=2048,
                ipr_samples=1024,
                pass_depth=0,
                pass_position=0,
                pixel_filter="box",
                filter_width=1,
            ),
        )
        self.target_ids = devices if isinstance(devices, list) else [devices]
        self.heavy = None
        self.references = {}
        self.recentered = False
        self.set_case("equirectangular")

    def set_case(self, case, *, offset=0, steps=3, pixel_aspect=1):
        self.case = case
        self.offset = offset
        self.steps = steps
        self.pixel_aspect = pixel_aspect
        self.recentered = False
        controls = (
            "focal",
            "aperture",
            "projection",
            "orthowidth",
            "near",
            "far",
            "cycles_panorama_type",
            "cycles_fisheye_fov",
            "cycles_fisheye_lens",
            "cycles_longitude_min",
            "cycles_longitude_max",
            "cycles_latitude_min",
            "cycles_latitude_max",
            "cycles_cylindrical_u_min",
            "cycles_cylindrical_u_max",
            "cycles_cylindrical_v_min",
            "cycles_cylindrical_v_max",
            "cycles_cylindrical_radius",
            "cycles_sensor_fit",
            "cycles_sensor_width",
            "cycles_sensor_height",
            "cycles_focus_distance",
            "cycles_fstop",
        ) + tuple("cycles_fisheye_k" + str(i) for i in range(5))
        for name in controls:
            pass_fixture.parm(self.camera, name).deleteAllKeyframes()
        pass_fixture.set_parameters(
            self.camera,
            dict(
                focal=50,
                aperture=120,
                projection=0,
                orthowidth=8,
                near=0.01,
                far=1000,
                cycles_projection="panorama",
                cycles_panorama_type="equirectangular",
                cycles_sensor_fit="horizontal",
                cycles_sensor_width=36,
                cycles_sensor_height=24,
                cycles_fisheye_fov=180,
                cycles_fisheye_lens=10.5,
                cycles_cylindrical_radius=1,
                cycles_dof=0,
                test_intrinsic_amplitude=1,
                cycles_focus_distance=4,
                cycles_fstop=125,
            ),
        )
        pass_fixture.set_parameters(
            self.rop,
            dict(
                motion_samples=steps,
                shutteroffset=offset,
                pixel_aspect=pixel_aspect,
                allowmotionblur=int(case != "motion_off"),
                pass_motion=int(case == "motion_off"),
            ),
        )

        def expr(name, text):
            pass_fixture.parm(self.camera, name).setExpression(text, hou.exprLanguage.Hscript)

        for name, text in (
            ("cycles_longitude_min", "-180+$T*1800"),
            ("cycles_longitude_max", "180+$T*3600"),
            ("cycles_latitude_min", "-60+$T*240"),
            ("cycles_latitude_max", "60+$T*480"),
        ):
            expr(name, text)
        if case in ("perspective", "extra_fov", "clipping", "motion_off"):
            pass_fixture.parm(self.camera, "cycles_projection").set("native")
            expr("focal", '120/(4*(.6+$T*19.2*ch("test_intrinsic_amplitude")))')
        if case == "motion_off":
            expr("focal", "120/(4*(.6+$T*4.8))")
        if case == "extra_fov":
            expr("focal", '120/(4*(.6+.15*sin($T*17280)*ch("test_intrinsic_amplitude")))')
        if case in ("orthographic", "ortho_clip", "bad_width"):
            pass_fixture.set_parameters(self.camera, dict(cycles_projection="native", projection=1))
            expr("orthowidth", "8+$T*192")
        if case in ("clipping", "projection_hold"):
            expr("near", "3+$T*96")
        if case == "ortho_clip":
            pass_fixture.parm(self.camera, "orthowidth").deleteAllKeyframes()
            pass_fixture.parm(self.camera, "orthowidth").set(8)
            expr("far", "3+$T*96")
        if case == "bad_width":
            pass_fixture.parm(self.camera, "orthowidth").setExpression(
                "float('nan') if abs(hou.time())>1e-7 else 8+hou.time()*192", hou.exprLanguage.Python
            )
        if case in ("fisheye_equidistant", "fisheye_equisolid", "sensor", "bad_lens", "bad_sensor", "fit_hold"):
            pass_fixture.parm(self.camera, "cycles_panorama_type").set(
                "fisheye_equidistant" if case == "fisheye_equidistant" else "fisheye_equisolid"
            )
            expr("cycles_fisheye_fov", "210+$T*2400")
            expr("cycles_fisheye_lens", "10.5+$T*96")
            expr("cycles_sensor_width", "36+$T*480")
        if case == "sensor":
            pass_fixture.parm(self.camera, "cycles_sensor_fit").set("vertical")
            expr("cycles_sensor_height", "24+$T*480")
        if case == "polynomial":
            pass_fixture.parm(self.camera, "cycles_panorama_type").set("fisheye_lens_polynomial")
            for i, value in enumerate((0, -0.05, 0, 0, 0)):
                pass_fixture.parm(self.camera, "cycles_fisheye_k" + str(i)).set(value)
            expr("cycles_fisheye_k1", "-.05-$T*.48")
            expr("cycles_fisheye_k3", "$T*.0001")
            expr("cycles_sensor_width", "36+$T*480")
        if case in ("cylinder", "cylinder_radius"):
            pass_fixture.parm(self.camera, "cycles_panorama_type").set("central_cylindrical")
            for name, text in (
                ("u_min", "-120+$T*1200"),
                ("u_max", "120+$T*2400"),
                ("v_min", "-1+$T*12"),
                ("v_max", "1+$T*24"),
            ):
                expr("cycles_cylindrical_" + name, text)
        if case == "cylinder_radius":
            for name, value in (("v_min", -1), ("v_max", 1)):
                parm = pass_fixture.parm(self.camera, "cycles_cylindrical_" + name)
                parm.deleteAllKeyframes()
                parm.set(value)
            expr("cycles_cylindrical_radius", "1/(1+$T*12)")
        if case == "bad_clip":
            pass_fixture.parm(self.camera, "near").setExpression(
                "float('nan') if abs(hou.time())>1e-7 else .01", hou.exprLanguage.Python
            )
        if case in ("bad_range", "dof_independent"):
            pass_fixture.parm(self.camera, "cycles_longitude_max").setExpression(
                "float('nan') if abs(hou.time())>1e-7 else 180+hou.time()*3600", hou.exprLanguage.Python
            )
        if case == "bad_lens":
            pass_fixture.parm(self.camera, "cycles_fisheye_lens").setExpression(
                "float('nan') if abs(hou.time())>1e-7 else 10.5+hou.time()*96", hou.exprLanguage.Python
            )
        if case == "bad_sensor":
            pass_fixture.parm(self.camera, "cycles_sensor_width").setExpression(
                "1e30 if abs(hou.time())>1e-7 else 36+hou.time()*480", hou.exprLanguage.Python
            )
            assert pass_fixture.parm(self.camera, "cycles_sensor_width").evalAtTime(-DURATION / 2) > 1e20, (
                "Sensor overflow witness was clamped"
            )
        if case == "fit_hold":
            pass_fixture.parm(self.camera, "cycles_sensor_fit").setExpression(
                "'vertical' if abs(hou.time())>1e-7 else 'horizontal'", hou.exprLanguage.Python
            )
        if case == "projection_hold":
            pass_fixture.parm(self.camera, "cycles_panorama_type").setExpression(
                "'mirrorball' if abs(hou.time())>1e-7 else 'equirectangular'", hou.exprLanguage.Python
            )
        if case == "inactive_controls":
            for name in ("focal", "cycles_fisheye_lens", "cycles_sensor_width", "cycles_fisheye_k1"):
                pass_fixture.parm(self.camera, name).setExpression("float('nan')", hou.exprLanguage.Python)
        if case == "dof_independent":
            pass_fixture.parm(self.camera, "cycles_dof").set(1)
            expr("cycles_focus_distance", "4+$T*48")

    def expected_codes(self):
        return (
            {"camera.shutter_clipping"}
            if self.case == "bad_clip"
            else {"camera.shutter_orthographic_scale"}
            if self.case == "bad_width"
            else {"camera.shutter_sensor_fit"}
            if self.case == "fit_hold"
            else {"camera.shutter_projection"}
            if self.case == "projection_hold"
            else {"camera.shutter_panorama"}
            if self.case in ("bad_range", "bad_lens", "bad_sensor", "dof_independent")
            else set()
        )

    def expected_samples(self):
        return 0 if self.case == "bad_width" else self.steps

    def recenter_final(self):
        # Only optics are frame-local. Independent scene/camera motion remains
        # absolute, and the ray reference includes that nominal origin.
        for parm in self.camera.parms():
            if parm.name() in ("tx", "ty", "tz", "rx", "ry", "rz"):
                continue
            try:
                expression = parm.expression()
                language = parm.expressionLanguage()
            except hou.OperationFailed:
                continue
            if language == hou.exprLanguage.Hscript:
                expression = expression.replace("$T", "($T-(floor($FF+.5)-1)/24)")
            else:
                expression = expression.replace("hou.time()", "(hou.time()-(int(hou.frame()+.5)-1)/24)")
            parm.setExpression(expression, language)
        self.recentered = True

    def reference(self):
        amplitude = pass_fixture.evaluate(self.camera, "test_intrinsic_amplitude")
        key = (self.case, self.offset, amplitude, self.pixel_aspect, hou.time())
        if key not in self.references:
            self.references[key] = exposure_reference(
                self.case,
                self.width,
                self.height,
                offset=self.offset,
                amplitude=amplitude,
                pixel_aspect=self.pixel_aspect,
                now=hou.time(),
                blur=self.case != "motion_off",
            )
        return self.references[key]

    def inspect(self, frame, tolerance=0.003):
        actual = plane(frame)[:, :, :3]
        expected = self.reference()
        difference = float(np.abs(actual - expected).mean())
        result = dict(
            mean_difference=difference,
            tolerance=tolerance,
            reference="Independent authored ray equations, colored-box intersections and exposure/pixel integration",
        )
        self.last_inspection = result
        assert np.isfinite(actual).all() and actual.max() > 0.1 and difference < tolerance, result
        return result

    def capture_info(self, session):
        return ch._native.snapshot_info(session._controller.acknowledged)

    def update(self, camera_only=False):
        update = native_scene.capture(self.rop, camera_only=camera_only)
        update["passes"] = ["combined", "motion"] if self.case == "motion_off" else ["combined"]
        return update

    def add_heavy(self):
        self.heavy = hou.node("/obj").createNode("geo", "intrinsic_retained_instances", run_init_scripts=False)
        object_properties.install(self.heavy)
        pass_fixture.parm(self.heavy, "cycles_geometry_motion").set("velocity")
        self.prototype = self.heavy.createNode("torus", "million_triangle_prototype")
        pass_fixture.set_parameters(self.prototype, dict(type=0, rows=512, cols=1024))
        targets = self.heavy.createNode("grid", "targets")
        pass_fixture.set_parameters(targets, dict(type=0, rows=3, cols=4))
        targets.parmTuple("t").set((18, 0, -8))
        copies = self.heavy.createNode("copytopoints", "twelve_instances")
        copies.setInput(0, self.prototype)
        copies.setInput(1, targets)
        pass_fixture.parm(copies, "pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        pass_fixture.parm(self.rop, "vobject").set(pass_fixture.evaluate(self.rop, "vobject") + " " + self.heavy.path())
