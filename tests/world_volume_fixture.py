# SPDX-License-Identifier: Apache-2.0
"""Unbounded world medium in front of an emissive native grid at distance three."""

from support.paths import artifacts_root
import os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene, registry


class Fixture:
    color = np.array((0.8, 0.4, 0.2))

    def __init__(self, device):
        self.device = device
        self.dest = artifacts_root() / "stage3/world-volume"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.root = hou.node("/obj").createNode("subnet", "world_volume_fixture")
        self.geo = self.root.createNode("geo", "emissive_plane", run_init_scripts=False)
        self.sop = self.geo.createNode("grid")
        self.sop.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.sop.parmTuple("size").set((2, 2))
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.camera = self.root.createNode("cam", "camera")
        self.camera.setParms(dict(tz=3, projection=1, orthowidth=4, near=0.0001, far=100))
        self.material = ch.create_material(name="world_volume_plane", shader="emission")
        self.material.node("surface_shader").parmTuple("color").set(tuple(self.color))
        self.material.node("surface_shader").parm("strength").set(1)
        self.geo.parm("shop_materialpath").set(self.material.path())
        self.world = ch.create_world_material(name="world_volume")
        self.background = self.world.node("background_shader")
        self.background.parmTuple("color").set((0.1, 0.2, 0.4))
        self.background.parm("strength").set(1)
        self.medium = self.world.createNode("cycles::absorption_volume::1", "world_medium")
        self.medium.parmTuple("color").set((0, 0, 0))
        self.medium.parm("density").set(0.4)
        self.output = self.world.node("world_output")
        self.output.setNamedInput("volume", self.medium, "volume")
        self.world.layoutChildren()
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        self.rop.setParms(
            dict(
                objectroot=self.root.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=96,
                resy=48,
                samples=256,
                ipr_samples=256,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                allowmotionblur=0,
                render_threads=2,
                use_texture_cache=0,
                world_source="material",
                world_material=self.world.path(),
            )
        )
        self.rop.parmTuple("background").set((0.025, 0.05, 0.1))

    def session(self):
        configuration = native_scene.configuration(self.rop)
        self.entry = registry.for_rop(self.rop, configuration)
        self.entry.session.close()
        self.entry.session = registry.session_from_configuration(configuration, collect_diagnostics=True)
        return self.entry

    def update(self, entry):
        now = hou.time()
        return entry.session.wait(entry.apply(native_scene.capture(self.rop, now), now), timeout=60)

    @staticmethod
    def pixels(frame, name="combined"):
        field = frame["planes"][name]
        return np.frombuffer(field["pixels"], np.float32).reshape(frame["height"], frame["width"], field["channels"])

    def check(self, frame, density, *, background=None, tolerance=0.008, distance=3, emission=None):
        rgb = self.pixels(frame)[:, :, :3]
        # Parallel orthographic rays cross three world units. Near clip is only
        # 1e-4; its effect is far below this stochastic volume tolerance.
        transmission = np.exp(-distance * density)
        expected = self.color * transmission
        if emission is not None:
            expected = expected + np.array(emission) * ((1 - transmission) / density if density else distance)
        measured = rgb[20:28, 44:52].mean(axis=(0, 1))
        np.testing.assert_allclose(measured, expected, atol=tolerance, rtol=0)
        if background is not None:
            np.testing.assert_allclose(rgb[0, 0], background, atol=0.0001, rtol=0)
        return measured.tolist()
