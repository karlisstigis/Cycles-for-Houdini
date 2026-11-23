from support.paths import artifacts_root
import pass_fixtures as pass_fixture

# SPDX-License-Identifier: Apache-2.0
from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

CASES = ("normal", "world", "position", "authored_normal")


class Fixture:
    def __init__(self, device):
        hou.setFps(24)
        hou.setFrame(1)
        self.dest = artifacts_root() / "examples/sampled-displacement"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="sampled_displacement", shader="emission")
        self.source = self.material.node("surface_shader")
        self.source.parmTuple("color").set((0.2, 0.4, 0.8))
        pass_fixture.parm(self.source, "strength").set(1)
        self.scalar = self.material.createNode("cycles::displacement::1", "normal_offset")
        pass_fixture.set_parameters(self.scalar, dict(height=0.5, midlevel=0, scale=1, space="object"))
        self.vector = self.material.createNode("cycles::vector_displacement::1", "world_offset")
        pass_fixture.set_parameters(self.vector, dict(midlevel=0, scale=1, space="world"))
        self.vector.parmTuple("vector").set((0, 0, 0.5))
        self.geometry = self.material.createNode("cycles::geometry::1", "original_position")
        self.multiply = self.material.createNode("cycles::vector_math::1", "position_offset")
        pass_fixture.parm(self.multiply, "math_type").set("multiply")
        self.multiply.parmTuple("vector2").set((0.5, 0, 0))
        self.multiply.setNamedInput("vector1", self.geometry, "position")
        self.local_vector = self.material.createNode("cycles::vector_displacement::1", "local_position_offset")
        pass_fixture.set_parameters(self.local_vector, dict(midlevel=0, scale=1, space="object"))
        self.local_vector.setNamedInput("vector", self.multiply, "vector")
        self.obj = hou.node("/obj").createNode("geo", "sampled_surface", run_init_scripts=False)
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.material.path())
        grid = self.obj.createNode("grid")
        pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
        grid.parmTuple("size").set((1, 1))
        reverse = self.obj.createNode("reverse")
        reverse.setInput(0, grid)
        self.sop = self.obj.createNode("attribwrangle", "OUT")
        self.sop.setInput(0, reverse)
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "sampled_camera")
        pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4, shutter=0.5))
        self.rop = hou.node("/out").createNode("cycles_ipr", "sampled_displacement")
        set_rop_devices(self.rop, device)
        # The default this test was written for (before the ROP took Blender's defaults).
        self.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=camera.path(),
                resx=160,
                resy=90,
                samples=1024,
                ipr_samples=1024,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                pass_position=1,
                pass_normal=1,
                pass_depth=1,
            ),
        )
        for name in ("adaptive_sampling", "ipr_adaptive_sampling"):
            parm = pass_fixture.parm(self.rop, name)
            if parm:
                parm.set(0)
        self.rop.parmTuple("background").set((0, 0, 0))
        pt = hou.IntParmTemplate("test_broken", "Evaluate Displacement", 1, default_value=(0,), min=0, max=1)
        pt.setScriptCallback(
            "import hou; hou.node(%r).parm('cycles_displacement_method').set('true' if kwargs['node'].evalParm('test_broken') else 'bump')"
            % self.material.path()
        )
        pt.setScriptCallbackLanguage(hou.scriptLanguage.Python)
        self.source.addSpareParmTuple(pt)
        self.parm = pass_fixture.parm(self.source, "test_broken")
        self.configure(CASES[0])

    def configure(self, case):
        self.case = case
        pass_fixture.parm(self.obj, "ry").deleteAllKeyframes()
        pass_fixture.parm(self.obj, "ry").set(0)
        pass_fixture.parm(self.sop, "snippet").set("")
        if case == "normal":
            pass_fixture.parm(self.sop, "snippet").set("@P.z=@P.x*(@Time*48);")
        elif case == "authored_normal":
            pass_fixture.parm(self.sop, "snippet").set("v@N=normalize(set(@Time*48,0,1));")
        elif case == "position":
            pass_fixture.parm(self.sop, "snippet").set("@P.x+=@Time*48;")
        else:
            pass_fixture.parm(self.obj, "ry").setExpression("$T*2160", hou.exprLanguage.Hscript)
        node = self.vector if case == "world" else self.local_vector if case == "position" else self.scalar
        self.material.node("surface_output").setNamedInput("displacement", node, "displacement")
        self.edit(False)

    def edit(self, enabled):
        self.parm.set(int(enabled))
        self.parm.pressButton()

    def check(self, frame, status, enabled, baseline=None):
        assert not status["error"] and not status["source_diagnostics"], status
        assert status["geometry_resources"] == status["object_resources"] == 1, status
        planes = {}
        for name, p in frame["planes"].items():
            values = np.frombuffer(p["pixels"], np.float32).reshape(90, 160, p["channels"])
            assert np.isfinite(values).all(), name
            planes[name] = values.copy()
        assert planes["combined"][:, :, :3].max() > 0.1
        if enabled and self.case in ("normal", "authored_normal"):
            occupied = planes["position"][:, :, 2] > 0.1
            assert occupied.sum() > 300 and float(planes["position"][:, :, 2][occupied].mean()) > 0.38
        if enabled and self.case == "world":
            visible = planes["combined"][:, :, 2] > 0.1
            assert abs(float(planes["position"][:, :, 2][visible].mean()) - 0.5) < 0.025
        if not enabled and baseline is not None:
            assert np.allclose(planes["combined"], baseline["combined"], atol=1e-5), (
                "Displacement mode repair changed the undisplaced motion"
            )
        return planes
