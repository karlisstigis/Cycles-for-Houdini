# SPDX-License-Identifier: Apache-2.0
"""Native packed deformation with independently known shutter coverage."""

from support.paths import artifacts_root
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch


class Fixture:
    color = np.array((0.8, 0.2, 0.05))
    width, height = 160, 90

    def __init__(self, device="CPU"):
        self.device = device
        self.dest = artifacts_root() / "stage2/motion/packed-deformation"
        self.dest.mkdir(parents=True, exist_ok=True)
        hou.setFps(24)
        hou.setFrame(1)
        self.obj = hou.node("/obj").createNode("geo", "packed_deformation", run_init_scripts=False)
        self.camera = hou.node("/obj").createNode("cam", "packed_deformation_camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.grid = self.obj.createNode("grid", "coarse_prototype")
        self.grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.grid.parmTuple("size").set((0.4, 1.2))
        self.point = self.obj.createNode("add", "point_prototype")
        self.point.parm("points").set(1)
        self.hair = self.obj.createNode("line", "hair_prototype")
        self.hair.parmTuple("origin").set((0, -0.6, 0))
        self.hair.parm("dist").set(1.2)
        self.motion = self.obj.createNode("attribwrangle", "animate_shape")
        self.motion.setInput(0, self.grid)
        self.motion.addSpareParmTuple(hou.FloatParmTemplate("test_speed", "Motion Scale", 1, default_value=(1.0,)))
        self.motion.addSpareParmTuple(hou.IntParmTemplate("test_broken", "Break Shutter Topology", 1))
        self.motion.addSpareParmTuple(hou.IntParmTemplate("test_kind", "Test Geometry Kind", 1))
        self.motion.addSpareParmTuple(hou.IntParmTemplate("test_rows", "Prototype Rows", 1, default_value=(2,)))
        self.motion.parm("snippet").set('@P.x += @Time*48*chf("test_speed")*(1+@P.y);')
        self.grid.parm("rows").setExpression(
            'if(ch("../animate_shape/test_broken")==1 && $FF>rint($FF),3,ch("../animate_shape/test_rows"))',
            hou.exprLanguage.Hscript,
        )
        self.grid.parm("cols").setExpression('ch("../animate_shape/test_rows")', hou.exprLanguage.Hscript)
        point_source = self.point
        empty = self.obj.createNode("add", "empty_shutter_source")
        self.point = self.obj.createNode("switch", "point_shutter_source")
        self.point.setInput(0, point_source)
        self.point.setInput(1, empty)
        self.point.parm("input").setExpression(
            'ch("../animate_shape/test_broken")==1 && ch("../animate_shape/test_kind")==1 && $FF>rint($FF)',
            hou.exprLanguage.Hscript,
        )
        self.hair.parm("points").setExpression(
            'if(ch("../animate_shape/test_broken")==1 && ch("../animate_shape/test_kind")==2 && $FF>rint($FF),3,2)',
            hou.exprLanguage.Hscript,
        )
        self.pack = self.obj.createNode("pack", "packed_animated_shape")
        self.pack.setInput(0, self.motion)
        self.pack.parm("pivot").set("origin")
        self.targets = self.obj.createNode("grid", "instance_positions")
        self.targets.setParms(dict(rows=2, cols=2, orient=0))
        self.targets.parmTuple("size").set((1.8, 0.8))
        # Copy to Points otherwise rotates copies onto this grid's -Z normal.
        # These analytic fixtures intentionally keep prototype +X in world +X.
        self.orient = self.obj.createNode("attribwrangle", "instance_orientation")
        self.orient.setInput(0, self.targets)
        self.orient.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
        self.copies = self.obj.createNode("copytopoints", "shared_instances")
        self.copies.setInput(0, self.pack)
        self.copies.setInput(1, self.orient)
        self.nested = self.obj.createNode("pack", "nested_shared_instances")
        self.nested.setInput(0, self.copies)
        self.nested.parm("pivot").set("origin")
        point_offset = self.obj.createNode("xform", "separate_point")
        point_offset.setInput(0, self.point)
        point_offset.parm("tx").set(-1.2)
        hair_offset = self.obj.createNode("xform", "separate_hair")
        hair_offset.setInput(0, self.hair)
        hair_offset.parm("tx").set(1.2)
        mixed = self.obj.createNode("merge", "mixed_prototype")
        mixed.setInput(0, self.motion)
        mixed.setInput(1, point_offset)
        mixed.setInput(2, hair_offset)
        self.mixed_pack = self.obj.createNode("pack", "packed_mixed_components")
        self.mixed_pack.setInput(0, mixed)
        self.mixed_pack.parm("pivot").set("origin")
        self.same_level = self.obj.createNode("merge", "packed_and_unpacked")
        self.same_level.setInput(0, self.motion)
        self.same_level.setInput(1, self.copies)
        self.kind = "mesh"
        self.pack.setDisplayFlag(True)
        self.pack.setRenderFlag(True)
        self.obj.layoutChildren()

    def rop(self, sop=None, interactive_samples=128):
        from cycles_houdini import menu_actions, device_selection

        self.material = ch.create_material(name="packed_deformation_surface", shader="emission")
        self.shader = self.material.node("surface_shader")
        self.shader.parmTuple("color").set(tuple(float(v) for v in self.color))
        self.shader.parm("strength").set(1)
        self.obj.parm("shop_materialpath").set(self.material.path())
        for node in self.obj.children():
            if node.isDisplayFlagSet():
                node.setDisplayFlag(False)
            if node.isRenderFlagSet():
                node.setRenderFlag(False)
        source = sop or self.pack
        source.setDisplayFlag(True)
        source.setRenderFlag(True)
        rop = menu_actions.create_rop()
        device_selection.set_devices(rop, self.device)
        rop.setParms(
            dict(
                objectroot="/obj",
                vobject=self.obj.name(),
                lights="",
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=1024,
                ipr_samples=interactive_samples,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_texture_cache=0,
                render_threads=2,
                world_source="color",
                film_exposure=1,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                shutteroffset=0,
            )
        )
        rop.parmTuple("background").set((0, 0, 0))
        return rop

    def update(self, steps=3, offset=0, enabled=True, sop=None, now=0, **request):
        return dict(
            geometry=[
                ch.geometry_request(
                    self.obj, sop or self.pack, material="surface", motion="deformation", at_time=now, **request
                )
            ],
            camera_path=self.camera.path(),
            background=(0, 0, 0),
            motion=dict(enabled=enabled, fps=24, shutter_frames=0.5, offset=offset, samples=steps),
            materials=[dict(key="surface", kind="emission", color=tuple(self.color))],
        )

    def pixels(self, frame, plane="combined"):
        field = frame["planes"][plane]
        return np.frombuffer(field["pixels"], np.float32).reshape(frame["height"], frame["width"], field["channels"])

    def coverage(self, frame, x=0, y=0):
        pixels = self.pixels(frame)
        column = round((x / 4 + 0.5) * self.width)
        row = round((y / 2.25 + 0.5) * self.height)
        return float(pixels[row - 1 : row + 1, column - 1 : column + 1, 0].mean() / self.color[0])

    def select(self, kind):
        self.kind = kind
        self.motion.parm("test_kind").set(("mesh", "points", "curves").index(kind))
        self.motion.setInput(0, dict(mesh=self.grid, points=self.point, curves=self.hair)[kind])

    def check(self, frame, status, speed=1, offset=0, warning=False, now=0):
        assert status["state"] != "failed" and not status["error"], status
        assert status["render_device_ids"] == [self.device], status["render_device_ids"]
        assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
        assert status["object_resources"] >= 1 and np.isfinite(self.pixels(frame)).all()
        result = []
        for y in (0,) if self.kind == "points" else (-0.3, 0, 0.3):
            diameter = 0.2 if self.kind == "curves" else 0.4
            expected = min(1.0, diameter / (speed * (1 + y))) if not warning else 1.0
            x = now * 48 * y + offset * 0.5 * speed * (1 + y) if not warning else 0
            value = self.coverage(frame, x, y)
            assert abs(value - expected) < 0.04, (y, value, expected, status["source_diagnostics"])
            result.append(dict(y=y, measured=value, expected=expected))
        return result
