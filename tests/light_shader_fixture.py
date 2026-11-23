# SPDX-License-Identifier: Apache-2.0
"""Analytic light on a native diffuse plane, with independent photometric references."""

from support.paths import artifacts_root
import math, os
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import menu_actions, device_selection, native_scene, registry, light_properties

IES_FACTOR = 0.0706650768394


def ies_text(gain=1, values=None):
    candela = [gain / IES_FACTOR] * 5 if values is None else [value / IES_FACTOR for value in values]
    return (
        "IESNA:LM-63-2002\n[TEST] Authored analytic Cycles Houdini fixture\nTILT=NONE\n1 -1 1 5 1 1 2 0 0 0\n1 1 1\n0 45 90 135 180\n0\n"
        + " ".join(format(x, ".12g") for x in candela)
        + "\n"
    )


def write_image(path, color):
    pixels = np.broadcast_to(np.asarray(color, np.float32), (8, 16, 3)).copy()
    output = oiio.ImageOutput.create(str(path))
    assert output.open(str(path), oiio.ImageSpec(16, 8, 3, oiio.FLOAT))
    assert output.write_image(pixels) and output.close()


class Fixture:
    def __init__(self, device):
        self.device = device
        self.dest = artifacts_root() / "stage3/light-shaders"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.scene = hou.node("/obj").createNode("subnet", "light_shader_fixture")
        self.obj = self.scene.createNode("geo", "receiver", run_init_scripts=False)
        self.sop = self.obj.createNode("grid", "diffuse_receiver")
        self.sop.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.sop.parmTuple("size").set((8, 8))
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.parent = self.scene.createNode("null", "light_parent")
        self.light = self.scene.createNode(light_properties.LIGHT_TYPE, "emitter")
        self.light.setInput(0, self.parent)
        self.light.setParms(
            dict(
                cycles_light_type="point",
                tz=3,
                light_intensity=100,
                light_exposure=0,
                cycles_light_radius=0,
                normalizearea=1,
            )
        )
        # The light defaults follow Blender; these fixtures rely on a camera-visible 2 x 2 rectangle with a hard spot.
        self.light.setParms(
            dict(
                cycles_light_visibility_camera=1,
                cycles_light_area_shape="rectangle",
                areasizex=2,
                areasizey=2,
                cycles_light_spot_smooth=0,
            )
        )
        self.camera = self.scene.createNode("cam", "camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4, near=0.01, far=100))
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        self.rop.setParms(
            dict(
                objectroot=self.scene.path(),
                vobject="*",
                lights="*",
                camera=self.camera.path(),
                resx=64,
                resy=64,
                samples=256,
                ipr_samples=256,
                render_threads=2,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                allowmotionblur=0,
                use_texture_cache=0,
                material_kind="diffuse",
                use_material_assignments=0,
                roughness=0,
                world_source="color",
                # Persistent Data defaults off (Blender); sequences here check one retained session.
                persistent_data=1,
            )
        )
        self.rop.parmTuple("basecolor").set((1, 1, 1))
        self.rop.parmTuple("background").set((0, 0, 0))
        self.profile = self.dest / "profile.ies"
        self.profile.write_text(ies_text(), encoding="ascii")
        self.texture = self.dest / "color.exr"
        write_image(self.texture, (0.25, 0.5, 0.75))

    def session(self):
        config = native_scene.configuration(self.rop)
        self.entry = registry.for_rop(self.rop, config)
        self.entry.session.close()
        self.entry.session = registry.session_from_configuration(config, collect_diagnostics=True)
        return self.entry

    def update(self, capture=True):
        now = hou.time()
        generation = (
            self.entry.apply(native_scene.capture(self.rop, now), now) if capture else self.entry.session.apply({})
        )
        return self.entry.session.wait(generation, timeout=60)

    @staticmethod
    def pixels(frame, name="combined"):
        field = frame["planes"][name]
        return np.frombuffer(field["pixels"], np.float32).reshape(frame["height"], frame["width"], field["channels"])

    @staticmethod
    def expected(multiplier=1, light_x=0, camera_x=0, power=100):
        # Point power / (4*pi*r^2), then cosine and Lambertian reflectance / pi.
        distance = math.sqrt(3**2 + (light_x - camera_x) ** 2)
        return np.broadcast_to(np.array(multiplier, dtype=float), (3,)) * power * 3 / (4 * math.pi**2 * distance**3)

    def check(self, frame, multiplier=1, *, light_x=0, camera_x=0, power=100, tolerance=0.004):
        pixels = self.pixels(frame)
        assert np.isfinite(pixels).all()
        measured = pixels[31:33, 31:33, :3].mean((0, 1))
        np.testing.assert_allclose(
            measured, self.expected(multiplier, light_x, camera_x, power), rtol=0, atol=tolerance
        )
        return measured.tolist()

    def add_heavy_packed(self):
        obj = self.scene.createNode("geo", "resident_packed_geometry", run_init_scripts=False)
        torus = obj.createNode("torus", "million_triangle_prototype")
        torus.setParms(dict(type=0, rows=512, cols=1024))
        targets = obj.createNode("grid", "targets")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("size").set((8, 6))
        targets.parmTuple("t").set((18, 0, -8))
        copies = obj.createNode("copytopoints", "twelve_shared_prototypes")
        copies.setInput(0, torus)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        self.heavy = obj
        return copies
