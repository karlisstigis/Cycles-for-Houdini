# SPDX-License-Identifier: Apache-2.0
"""Numeric sky source faults with independent objects and displacement branches."""

import math
import hou
import numpy as np
from cycles_houdini import menu_actions
from shader_source_recovery_fixture import Fixture as SurfaceFixture

CASES = ("world_direction", "surface_color", "shared_height", "height_only", "normal_only", "unused")


class Fixture(SurfaceFixture):
    def __init__(self, device):
        super().__init__(device)
        self.device = device
        self.dest = self.dest.parent / "sky-source-recovery"
        self.dest.mkdir(exist_ok=True)
        self.world = menu_actions.create_sky_world()
        self.world_sky = self.world.node("sky_texture")
        self.camera = hou.node(self.rop.evalParm("camera"))
        self.background = (0.125, 0.25, 0.5)
        self.rop.setParms(
            dict(
                samples=64,
                ipr_samples=64,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                render_threads=2,
                film_exposure=1,
                use_texture_cache=0,
            )
        )
        self.configure(CASES[0])

    def configure(self, case):
        if case == "attribute":
            return super().configure(case)
        super().configure("attribute")
        self.case = case
        self.rop.setParms(dict(world_source="color", world_material=self.world.path()))
        self.rop.parmTuple("background").set(self.background)
        self.objects[0].parm("tz").set(0)
        self.camera.parm("tx").set(0)
        if case.startswith("world_"):
            self.source = self.world_sky
            self.source.parm("sky_type").set("hosek_wilkie")
            self.source.parmTuple("sun_direction").set((0, 0, 1))
            self.parm = self.source.parmTuple("sun_direction")[2]
            self.good, self.bad = 1, 0
            self.rop.parm("world_source").set("material")
        else:
            self.source = self.material.createNode("cycles::sky_texture::1", "sky")
            self.source.setParms(dict(sky_type="hosek_wilkie", turbidity=2.2))
            self.source.parmTuple("sun_direction").set((0, 0, 1))
            direction = self.material.createNode("cycles::combine_xyz::1", "direction")
            direction.setParms(dict(x=0.6, y=0, z=0.8))
            self.source.setNamedInput("vector", direction, "vector")
            self.parm = self.source.parmTuple("sun_direction")[2]
            self.good, self.bad = 1, 0
            height = self.material.node("height")
            if case in ("surface_color", "shared_height"):
                self.surface.setNamedInput("color", self.source, "color")
            if case in ("shared_height", "height_only"):
                bw = self.material.createNode("cycles::rgb_to_bw::1", "height_from_sky")
                bw.setNamedInput("color", self.source, "color")
                height.setNamedInput("height", bw, "val")
                height.parm("scale").set(0.01)
            if case == "normal_only":
                self.surface.destroy()
                self.surface = self.material.createNode("cycles::diffuse_bsdf::1", "surface_shader")
                self.surface.parmTuple("color").set((0.2, 0.6, 0.3))
                self.surface.parm("roughness").set(0)
                self.surface.setNamedInput("normal", self.source, "color")
                self.material.node("surface_output").setNamedInput("surface", self.surface, "BSDF")
        self.set_broken(False)

    def set_broken(self, broken):
        # Each component is within its UI range; the zero vector is unusable.
        self.parm.setExpression(str(self.bad if broken else self.good), hou.exprLanguage.Hscript)
        assert math.isclose(self.parm.eval(), self.bad if broken else self.good, rel_tol=1e-6)

    @staticmethod
    def plane(frame, name):
        p = frame["planes"][name]
        return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])

    def check(self, frame, status, broken, baseline=None, control_z=0, atol=2e-5):
        assert status["state"] != "failed" and not status["error"] and not status.get("update_rejected"), status
        assert status["object_resources"] == status["geometry_resources"] == 2, status
        planes = {name: self.plane(frame, name) for name in ("combined", "position", "normal")}
        assert all(np.isfinite(p).all() for p in planes.values())
        rgb, pos, normal = (planes[name] for name in ("combined", "position", "normal"))
        np.testing.assert_allclose(rgb[32, 32, :3], (0.2, 0.6, 0.3), atol=atol, rtol=0)
        assert abs(pos[32, 32, 2] - control_z) < 2e-5, pos[32, 32]
        assert rgb[32, 96, 3] > 0.99
        issues = status["source_diagnostics"]
        expected = broken and self.case != "unused"
        assert bool(issues) == expected, issues
        if expected:
            code = "world.unavailable" if self.case.startswith("world_") else "material.sky_unavailable"
            assert all(issue["code"] == code and "Sky Texture" in issue["message"] for issue in issues), issues
            if self.case.startswith("world_"):
                np.testing.assert_allclose(rgb[2, 2, :3], self.background, atol=atol, rtol=0)
            elif self.case in ("surface_color", "shared_height"):
                np.testing.assert_allclose(rgb[32, 96, :3], (1, 0, 1), atol=atol, rtol=0)
            elif self.case == "normal_only":
                np.testing.assert_allclose(normal[32, 96, :3], (0, 0, 1), atol=2e-5, rtol=0)
                assert rgb[32, 96, 1] > rgb[32, 96, 0], rgb[32, 96]
            else:
                np.testing.assert_allclose(rgb[32, 96, :3], (0.2, 0.6, 0.3), atol=atol, rtol=0)
            target = 0 if self.case in ("shared_height", "height_only") else 0.25
            assert abs(pos[32, 96, 2] - target) < 2e-5, (self.case, pos[32, 96])
        if baseline:
            for name, p in planes.items():
                np.testing.assert_allclose(
                    p, baseline[name], atol=max(atol, 2e-5), rtol=0, err_msg=self.case + "/" + name
                )
        return {name: p.copy() for name, p in planes.items()}
