# SPDX-License-Identifier: Apache-2.0
"""Native environment, independent radiance and real surface illumination."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as oiio

from cycles_houdini import light_properties, menu_actions, native_scene, registry


class Fixture:
    width, height = 160, 100
    radiance = np.asarray((0.25, 0.5, 0.75), np.float32)
    fallback = np.asarray((0.015625, 0.03125, 0.0625), np.float32)

    def __init__(self, device):
        self.device = device
        self.dest = artifacts_root() / "stage3/world/recovery"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.image = self.dest / "radiance.exr"
        self.write_image(self.image, self.radiance)
        hou.setFps(24)
        hou.setFrame(1)
        self.scene = hou.node("/obj").createNode("subnet", "environment_recovery")
        self.camera = self.scene.createNode("cam", "camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=6, shutter=0.5))
        self.geo = self.scene.createNode("geo", "receiver", run_init_scripts=False)
        self.grid = self.geo.createNode("grid", "surface")
        pass_fixture.set_parameters(self.grid, dict(type=0, orient=0, rows=2, cols=2))
        self.grid.parmTuple("size").set((2, 2))
        self.grid.setDisplayFlag(True)
        self.grid.setRenderFlag(True)
        self.env = self.scene.createNode("envlight", "environment")
        light_properties.install(self.env)
        pass_fixture.set_parameters(
            self.env,
            dict(
                env_map=str(self.image),
                light_contribprimary=1,
                light_intensity=1,
                light_exposure=0,
                cycles_environment_colorspace="data",
            ),
        )
        self.env.parmTuple("light_color").set((1, 1, 1))
        self.rop = menu_actions.create_rop()
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                objectroot=self.scene.path(),
                camera=self.camera.path(),
                vobject="receiver",
                lights="*",
                world_source="scene",
                resx=self.width,
                resy=self.height,
                samples=512,
                ipr_samples=512,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                material_kind="diffuse",
                use_material_assignments=0,
                roughness=0,
                film_exposure=1,
                allowmotionblur=0,
                render_threads=2,
                use_texture_cache=0,
                pass_position=1,
                pass_depth=1,
            ),
        )
        self.rop.parmTuple("basecolor").set((1, 1, 1))
        self.rop.parmTuple("background").set(self.fallback.tolist())
        self.entry = None

    @staticmethod
    def write_image(path, value):
        spec = oiio.ImageSpec(32, 16, 3, oiio.FLOAT)
        spec.attribute("oiio:ColorSpace", "data")
        output = oiio.ImageOutput.create(str(path))
        assert output.open(str(path), spec)
        assert output.write_image(np.broadcast_to(np.asarray(value, np.float32), (16, 32, 3)).copy())
        assert output.close()

    def session(self):
        config = native_scene.configuration(self.rop)
        self.entry = registry.for_rop(self.rop, config)
        self.entry.session.close()
        self.entry.session = registry.session_from_configuration(config, collect_diagnostics=True)
        return self.entry

    def render(self, at=None):
        at = hou.time() if at is None else at
        return self.entry.session.wait(self.entry.apply(native_scene.capture(self.rop, at), at))

    def planes(self, frame):
        result = {
            name: np.frombuffer(plane["pixels"], np.float32).reshape(self.height, self.width, plane["channels"]).copy()
            for name, plane in frame["planes"].items()
        }
        assert all(np.isfinite(value).all() for value in result.values())
        return result

    def check(self, frame, status, expected=None, settings=(), unavailable=False, motion=False, surface=True):
        expected = self.radiance if expected is None else np.asarray(expected)
        assert not status["error"] and status["state"] != "failed", status
        assert status["render_device_ids"] == [self.device], status
        planes = self.planes(frame)
        assert np.allclose(planes["combined"][2, 2, :3], expected, atol=0.0002, rtol=0), (
            planes["combined"][2, 2],
            expected,
            status["source_diagnostics"],
        )
        if surface:
            diffuse = planes["combined"][45:55, 75:85, :3].mean((0, 1))
            assert np.allclose(diffuse, expected, atol=0.025, rtol=0), (diffuse, expected)
        # A white Lambertian under a uniform world has the same radiance as
        # the background. Independently require actual visible surface samples.
        assert 0.1 < float(np.median(planes["depth"][45:55, 75:85])) < 20
        issues = status["source_diagnostics"]
        expected_codes = {"world.unsupported_setting"} if settings else set()
        if unavailable:
            expected_codes.add("world.unavailable")
        if motion:
            expected_codes.add("world.motion_ignored")
        assert {issue["code"] for issue in issues} == expected_codes, issues
        optional = [issue for issue in issues if issue["code"] == "world.unsupported_setting"]
        assert len(optional) == len(settings), (settings, optional)
        assert len({(issue["source"], issue["message"]) for issue in optional}) == len(optional)
        for parameter in settings:
            assert any(parameter in issue["message"] for issue in optional), (parameter, optional)
        for issue in optional:
            assert issue["source"] == self.env.path() and issue["resource"] == "world"
            assert issue["severity"] == "warning" and "retaining" in issue["action"].lower()
        return planes
