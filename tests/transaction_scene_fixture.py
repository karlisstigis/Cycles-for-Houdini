# SPDX-License-Identifier: Apache-2.0
"""Fresh native scene with independently specified surface/background radiance."""

from support.paths import artifacts_root
import os
from pathlib import Path
import time

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene


class Fixture:
    color = (0.2, 0.4, 0.8)
    background = (0.03, 0.06, 0.09)
    world_color = (0.12, 0.10, 0.08)

    def __init__(self, device="CPU", heavy=False):
        self.width, self.height = 128, 96
        self.dest = artifacts_root() / "stage3/transactions" / str(time.time_ns())
        self.dest.mkdir(parents=True)
        hou.setFps(24)
        hou.setFrame(1)
        self.scene = hou.node("/obj").createNode("subnet", "transaction_scene")
        self.geo = self.scene.createNode("geo", "receiver", run_init_scripts=False)
        self.grid = self.geo.createNode("grid", "plane")
        self.grid.setParms(dict(type=0, orient=0, rows=721 if heavy else 2, cols=721 if heavy else 2))
        self.grid.parmTuple("size").set((1.6, 1.6))
        self.sop = self.geo.createNode("attribwrangle", "uv")
        self.sop.setInput(0, self.grid)
        self.sop.parm("snippet").set("v@uv=set(@P.x/1.6+.5,@P.y/1.6+.5,0);")
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.camera = self.scene.createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.material = ch.create_material(name="transaction_surface", shader="emission")
        self.shader = self.material.node("surface_shader")
        self.shader.parm("strength").set(1)
        self.texture = self.material.createNode("cycles::image_texture::1", "color")
        self.texture.setParms(
            dict(
                filename=self.write_image(self.dest / "color.exr", self.color),
                colorspace="data",
                interpolation="closest",
                tiles="",
            )
        )
        self.shader.setNamedInput("color", self.texture, "color")
        self.geo.parm("shop_materialpath").set(self.material.path())
        self.material.parm("cycles_pass_index").set(3)
        self.world = ch.create_world_material(name="transaction_world")
        background = next(node for node in self.world.children() if node.parmTuple("color") and node.parm("strength"))
        background.parmTuple("color").set(self.world_color)
        background.parm("strength").set(1)
        self.foreign = hou.node("/mat").createNode("principledshader::2.0", "unfamiliar_surface")
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        self.rop.setParms(
            dict(
                objectroot=self.scene.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=4,
                ipr_samples=4,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                film_exposure=1,
                world_source="color",
                world_sampling="none",
                use_texture_cache=0,
                render_threads=2,
                allowmotionblur=0,
            )
        )
        self.rop.parmTuple("background").set(self.background)

    @staticmethod
    def write_image(path, color):
        pixels = np.broadcast_to(np.asarray(color, dtype=np.float32), (8, 8, 3)).copy()
        output = oiio.ImageOutput.create(str(path))
        assert output.open(str(path), oiio.ImageSpec(8, 8, 3, oiio.FLOAT)), output.geterror()
        assert output.write_image(pixels) and output.close(), output.geterror()
        return str(path)

    def capture(self):
        result = native_scene.capture(self.rop)
        result["passes"] = ["combined", "depth", "position", "material_id"]
        return result

    @staticmethod
    def plane(frame, name):
        plane = frame["planes"][name]
        return np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])

    def check(self, frame, color=None, background=None, *, atol=2e-5):
        pixels = self.plane(frame, "combined")
        assert np.isfinite(pixels).all()
        color = self.color if color is None else color
        background = self.background if background is None else background
        np.testing.assert_allclose(pixels[self.height // 2, self.width // 2, :3], color, atol=atol, rtol=0)
        np.testing.assert_allclose(pixels[2, 2, :3], background, atol=atol, rtol=0)
        if "position" in frame["planes"]:
            assert abs(float(self.plane(frame, "position")[self.height // 2, self.width // 2, 2])) < 1e-5
        return dict(center=pixels[self.height // 2, self.width // 2, :3].tolist(), corner=pixels[2, 2, :3].tolist())
