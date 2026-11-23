# SPDX-License-Identifier: Apache-2.0
"""One dense native prototype with per-instance surface material styles."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene


class Fixture:
    width, height = 192, 144
    colors = ((0.2, 0.4, 0.6), (0.8, 0.1, 0.2), (0.1, 0.7, 0.3), (0.6, 0.2, 0.8))

    def __init__(self, device="CPU", heavy=True):
        self.dest = artifacts_root() / "stage3/stylesheet-storage"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.scene = hou.node("/obj").createNode("subnet", "stylesheet_storage")
        self.obj = self.scene.createNode("geo", "styled_instances", run_init_scripts=False)
        self.prototype = self.obj.createNode("grid", "shared_prototype")
        pass_fixture.set_parameters(
            self.prototype, dict(type=0, orient=0, rows=513 if heavy else 3, cols=1025 if heavy else 3)
        )
        self.prototype.parmTuple("size").set((1.8, 1.6))
        self.targets = self.obj.createNode("grid", "twelve_targets")
        pass_fixture.set_parameters(self.targets, dict(type=0, orient=0, rows=3, cols=4))
        self.targets.parmTuple("size").set((9, 6))
        self.orientation = self.obj.createNode("attribwrangle", "orientation")
        self.orientation.setInput(0, self.targets)
        pass_fixture.parm(self.orientation, "snippet").set("v@N={0,0,1};v@up={0,1,0};")
        self.copies = self.obj.createNode("copytopoints", "packed_copies")
        self.copies.setInput(0, self.prototype)
        self.copies.setInput(1, self.orientation)
        pass_fixture.parm(self.copies, "pack").set(1)
        self.copies.setRenderFlag(True)
        self.copies.setDisplayFlag(True)
        self.material = ch.create_material(name="storage_surface", shader="emission")
        self.material.node("surface_shader").parmTuple("color").set(self.colors[0])
        pass_fixture.parm(self.material.node("surface_shader"), "strength").set(1)
        pass_fixture.parm(self.material, "cycles_pass_index").set(3)
        ch.promote_parameter(self.material, self.material.node("surface_shader"), "color", name="tint")
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.material.path())
        self.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Style Sheet", 1))
        self.camera = self.scene.createNode("cam", "camera")
        pass_fixture.set_parameters(self.camera, dict(tz=12, projection=1, orthowidth=12))
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
                render_threads=2,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                allowmotionblur=0,
                use_texture_cache=0,
                world_source="color",
                crypto_material=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.mode = "uniform"
        self.offset = 0

    def style_text(self, mode="colors", offset=0, bad=False):
        rules = []
        if mode != "uniform":
            for number in range(12):
                value = list(self.colors[number % 4])
                value[0] += offset
                values = dict(tint=value)
                if bad and number == 1:
                    values["unrecognized_export"] = 1
                rules.append(dict(target=dict(group=str(number)), overrides=dict(materialParameters=values)))
        return json.dumps(dict(styles=rules)) if rules else ""

    def styles(self, mode="colors", offset=0, bad=False):
        self.mode, self.offset = mode, offset
        pass_fixture.parm(self.obj, "shop_materialstylesheet").set(self.style_text(mode, offset, bad))

    def update(self):
        value = native_scene.capture(self.rop)
        value["passes"] = ["combined", "position", "depth", "material_id"]
        return value

    def enable_material_motion(self, general_bindings=False, displacement=False):
        if general_bindings:
            authored = self.obj.createNode("attribwrangle", "primitive_materials")
            authored.setInput(0, self.prototype)
            pass_fixture.parm(authored, "class").set(1)
            authored.addSpareParmTuple(
                hou.StringParmTemplate("binding", "Binding", 1, default_value=(self.material.path(),))
            )
            pass_fixture.parm(authored, "snippet").set('s@shop_materialpath=chs("binding");')
            self.copies.setInput(0, authored)
            self.prototype = authored
        pass_fixture.set_parameters(self.rop, dict(allowmotionblur=1, geometry_motion="transform", motion_samples=3))
        pass_fixture.parm(self.camera, "shutter").set(0.5)
        if displacement:
            height = self.material.createNode("cycles::displacement::1", "profile_height")
            pass_fixture.set_parameters(height, dict(height=0.02, scale=1, midlevel=0, space="object"))
            self.material.node("surface_output").setNamedInput("displacement", height, "displacement")
            pass_fixture.parm(self.material, "cycles_displacement_method").set("true")

    def check(self, frame, offset=0):
        p = frame["planes"]["combined"]
        pixels = np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])
        assert np.isfinite(pixels).all()
        colors = np.unique(np.round(pixels[:, :, :3].reshape(-1, 3), 4), axis=0)
        expected = (
            [self.colors[0]] if self.mode == "uniform" else [tuple(np.asarray(c) + (offset, 0, 0)) for c in self.colors]
        )
        for color in expected:
            assert np.max(np.abs(colors - np.asarray(color)), axis=1).min() < 3e-4, (color, colors)
        if self.mode != "uniform" and "position" in frame["planes"] and "depth" in frame["planes"]:
            data = frame["planes"]["position"]
            positions = np.frombuffer(data["pixels"], np.float32).reshape(self.height, self.width, data["channels"])
            data = frame["planes"]["depth"]
            depth = np.frombuffer(data["pixels"], np.float32).reshape(self.height, self.width, data["channels"])[
                :, :, 0
            ]
            for number, point in enumerate(self.targets.geometry().points()):
                center = point.position() * self.obj.worldTransform()
                mask = (
                    (depth > 0)
                    & (depth < 100)
                    & (np.abs(positions[:, :, 0] - center[0]) < 0.85)
                    & (np.abs(positions[:, :, 1] - center[1]) < 0.75)
                )
                assert mask.sum() > 20, (number, mask.sum())
                np.testing.assert_allclose(
                    pixels[:, :, :3][mask],
                    np.broadcast_to(np.asarray(expected[number % 4]), (mask.sum(), 3)),
                    atol=3e-4,
                    rtol=0,
                )
        return colors.tolist()
