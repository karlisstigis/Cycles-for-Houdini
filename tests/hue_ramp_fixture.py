# SPDX-License-Identifier: Apache-2.0
"""Spatial Color Ramp samples with independent pinned Blender references."""

import pass_fixtures as pass_fixture

import json, os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, native_scene


class Fixture:
    def __init__(self, device, reference_path="blender-hue-ramps.json"):
        self.root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        self.reference = json.loads((self.root / "tests/fixtures" / reference_path).read_text())
        # Public ColorRamp.evaluate values remain in the fixture for editor
        # checks. Runtime/preview checks use Blender's 257-entry export path.
        path = Path(reference_path)
        tables = json.loads((self.root / "tests/fixtures" / (path.stem + "-tables.json")).read_text())
        self.reference["cases"] = [dict(case, **tables[case["id"]]) for case in self.reference["cases"]]
        self.cases = {case["id"]: case for case in self.reference["cases"]}
        self.factors = self.reference["cases"][0]["factors"]
        self.width = len(self.factors) * 4
        self.height = 8
        self.scene = hou.node("/obj").createNode("subnet", "hue_ramp_fixture")
        self.obj = self.scene.createNode("geo", "surface", run_init_scripts=False)
        self.stash = self.obj.createNode("stash", "FACTOR_BANDS")
        g = hou.Geometry()
        g.addAttrib(hou.attribType.Point, "hue_factor", 0.0)
        for i, factor in enumerate(self.factors):
            x0 = -2 + 4 * i / len(self.factors)
            x1 = -2 + 4 * (i + 1) / len(self.factors)
            points = []
            for position in ((x0, -1, 0), (x1, -1, 0), (x1, 1, 0), (x0, 1, 0)):
                p = g.createPoint()
                p.setPosition(position)
                p.setAttribValue("hue_factor", factor)
                points.append(p)
            face = g.createPolygon()
            for p in reversed(points):
                face.addVertex(p)
        pass_fixture.parm(self.stash, "stash").set(g)
        self.stash.setDisplayFlag(True)
        self.stash.setRenderFlag(True)
        self.mat = ch.create_material(name="hue_ramp", shader="emission")
        self.emission = self.mat.node("surface_shader")
        pass_fixture.parm(self.emission, "strength").set(1)
        self.ramp = self.mat.createNode("cycles::color_ramp::1", "color_ramp")
        self.attribute = self.mat.createNode("cycles::attribute::1", "spatial_factor")
        pass_fixture.parm(self.attribute, "attribute").set("hue_factor")
        self.ramp.setNamedInput("factor", self.attribute, "fac")
        self.emission.setNamedInput("color", self.ramp, "color")
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.mat.path())
        self.camera = self.scene.createNode("cam", "camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=4))
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        pass_fixture.set_parameters(
            self.rop,
            dict(
                objectroot=self.scene.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=1,
                ipr_samples=1,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                allowmotionblur=0,
                use_texture_cache=0,
                render_threads=2,
                pass_position=1,
                pass_normal=1,
                pass_depth=0,
                pass_uv=0,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.rop.parmTuple("lightstrength").set((0, 0, 0))
        self.choose(self.reference["cases"][0]["id"])

    def choose(self, name, basis=hou.rampBasis.Linear):
        self.case = self.cases[name]
        controls = self.case["controls"]
        keys = tuple(k["position"] for k in controls)
        pass_fixture.parm(self.ramp, "ramp").set(
            hou.Ramp((basis,) * len(controls), keys, tuple(tuple(k["color"]) for k in controls))
        )
        # Reference Alpha stops share the Color positions. The independent
        # Alpha ramp takes them with the same basis (Linear for HSV/HSL, whose
        # Alpha blends linearly); cases without Alpha stay opaque.
        alpha_basis = hou.rampBasis.Linear if self.case["mode"] in ("HSV", "HSL") else basis
        if all("alpha" in k for k in controls):
            pass_fixture.parm(self.ramp, "alpha_ramp").set(
                hou.Ramp((alpha_basis,) * len(controls), keys, tuple(k["alpha"] for k in controls))
            )
        else:
            pass_fixture.parm(self.ramp, "alpha_ramp").set(
                hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (1.0, 1.0))
            )
        pass_fixture.set_parameters(
            self.ramp, dict(ramp_color_space=self.case["mode"], ramp_hue_interpolation=self.case["hue"])
        )
        pass_fixture.parm(self.ramp, "ramp_interpolation").set(self.case.get("interpolation", "NATIVE"))

    def update(self):
        return native_scene.capture(self.rop)

    def planes(self, frame):
        return {
            k: np.frombuffer(v["pixels"], np.float32).reshape(self.height, self.width, v["channels"])
            for k, v in frame["planes"].items()
        }

    def inspect(self, frame, expected=None):
        planes = self.planes(frame)
        image = planes["combined"]
        assert np.isfinite(image).all()
        measured = image[self.height // 2, np.arange(len(self.factors)) * 4 + 2, :3]
        if expected is None:
            expected = np.asarray(self.case["colors"])
        expected = np.broadcast_to(expected, measured.shape)
        np.testing.assert_allclose(measured, expected, atol=3e-5, rtol=3e-5)
        assert np.all(image[self.height // 2, np.arange(len(self.factors)) * 4 + 2, 3] > 0.999)
        return float(np.max(np.abs(measured - expected)))

    def displace(self, shared=False):
        if self.mat.node("displacement") is None:
            self.displacement = self.mat.createNode("cycles::displacement::1", "displacement")
            pass_fixture.set_parameters(self.displacement, dict(height=0.25, midlevel=0, scale=1, space="object"))
        self.displacement = self.mat.node("displacement")
        self.displacement.setInput(0, None)
        if shared:
            self.displacement.setNamedInput("height", self.ramp, "color")
        self.mat.node("surface_output").setNamedInput("displacement", self.displacement, "displacement")
        pass_fixture.parm(self.mat, "cycles_displacement_method").set("both")

    def add_heavy(self):
        obj = self.scene.createNode("geo", "retained_heavy", run_init_scripts=False)
        self.heavy = obj.createNode("torus", "million_triangles")
        pass_fixture.set_parameters(self.heavy, dict(type=0, rows=512, cols=1024))
        targets = obj.createNode("grid")
        pass_fixture.set_parameters(targets, dict(type=0, rows=3, cols=4))
        targets.parmTuple("t").set((0, 0, 20))
        copies = obj.createNode("copytopoints")
        copies.setInput(0, self.heavy)
        copies.setInput(1, targets)
        pass_fixture.parm(copies, "pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        mat = ch.create_material(name="retained_hue_light", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        mat.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8)))
        pass_fixture.parm(mat.node("surface_shader"), "strength").set(1)
        pass_fixture.parm(obj, "shop_materialpath").set(mat.path())
        return obj
