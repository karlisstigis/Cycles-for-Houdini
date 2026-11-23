# SPDX-License-Identifier: Apache-2.0
"""Native shader editing fixture with independently specified image values."""

from support.paths import artifacts_root
import os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection


class Fixture:
    def __init__(self, device="CPU"):
        self.dest = artifacts_root() / "stage3/materials/bypass-preview"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.scene = hou.node("/obj").createNode("subnet", "shader_edit_fixture")
        self.geo = self.scene.createNode("geo", "receiver", run_init_scripts=False)
        grid = self.geo.createNode("grid")
        grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
        grid.parmTuple("size").set((8, 8))
        reverse = self.geo.createNode("reverse")
        reverse.setInput(0, grid)
        self.sop = self.geo.createNode("attribwrangle")
        self.sop.setInput(0, reverse)
        self.sop.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.camera = self.scene.createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.builder = ch.create_material(name="shader_bypass_preview", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.builder.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8)))
        self.output = self.builder.node("surface_output")
        self.emission = self.builder.node("surface_shader")
        self.emission.parm("strength").set(1)
        self.color = self.builder.createNode("cycles::color::1", "preview_color")
        self.color.parmTuple("value").set((0.2, 0.4, 0.8))
        self.value = self.builder.createNode("cycles::value::1", "preview_value")
        self.value.parm("value").set(0.3)
        self.math = self.builder.createNode("cycles::math::1", "multiply")
        self.math.setParms(dict(math_type="multiply", value2=2))
        self.math.setNamedInput("value1", self.value, "value")
        self.range = self.builder.createNode("cycles::map_range::1", "range")
        # The default this test was written for (new nodes now take Blender's).
        self.range.setParms(dict(clamp=0))
        self.range.setParms(dict(from_min=0, from_max=1, to_min=0, to_max=2))
        self.range.setNamedInput("value", self.value, "value")
        self.texture = self.builder.createNode("cycles::image_texture::1", "repairable_image")
        self.texture.setParms(dict(filename=str(self.dest / "missing.exr"), colorspace="data", interpolation="closest"))
        self.texture.parm("tiles").set("")
        self.geo.parm("shop_materialpath").set(self.builder.path())
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        self.rop.setParms(
            dict(
                objectroot=self.scene.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=64,
                resy=64,
                samples=8,
                ipr_samples=8,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                use_texture_cache=0,
                render_threads=2,
                world_source="color",
                allowmotionblur=0,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.output.setNamedInput("surface", self.math, "value")
        self.builder.layoutChildren()

    def image(self, rgba=(0.4, 0.4, 0.4, 0.75)):
        path = self.dest / "repaired.exr"
        pixels = np.broadcast_to(np.asarray(rgba, dtype=np.float32), (4, 4, 4)).copy()
        spec = OIIO.ImageSpec(4, 4, 4, OIIO.FLOAT)
        out = OIIO.ImageOutput.create(str(path))
        assert out.open(str(path), spec) and out.write_image(pixels) and out.close()
        return str(path)

    @staticmethod
    def measure(frame):
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(64, 64, 4)
        assert np.isfinite(pixels).all()
        return pixels[30:34, 30:34, :3].mean(axis=(0, 1))

    def expect(self, frame, expected):
        measured = self.measure(frame)
        assert np.allclose(measured, expected, atol=2e-5, rtol=0), (measured, expected)
        return measured.tolist()
