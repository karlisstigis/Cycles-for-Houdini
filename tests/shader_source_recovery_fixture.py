from support.paths import artifacts_root
import pass_fixtures as pass_fixture

# SPDX-License-Identifier: Apache-2.0
from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

CASES = (
    "attribute",
    "shared_height",
    "color_layer",
    "uv_map",
    "tangent",
    "normal_map",
    "vector_displacement",
    "normal_input",
)


class Fixture:
    def __init__(self, device):
        self.dest = artifacts_root() / "examples/shader-source-recovery"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="source_recovery", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        valid = ch.create_material(name="unaffected", shader="emission")
        valid.node("surface_shader").parmTuple("color").set((0.2, 0.6, 0.3))
        pass_fixture.parm(valid.node("surface_shader"), "strength").set(1)
        self.objects = []
        for index, x in enumerate((-1, 1)):
            obj = hou.node("/obj").createNode("geo", "source_recovery_" + str(index), run_init_scripts=False)
            pass_fixture.parm(obj, "tx").set(x)
            pass_fixture.parm(obj, "shop_materialpath").set((valid if not index else self.material).path())
            g = hou.Geometry()
            g.addAttrib(hou.attribType.Point, "uv", (0.0, 0.0, 0.0))
            g.addAttrib(hou.attribType.Point, "Cd", (0.2, 0.6, 0.3))
            poly = g.createPolygon()
            for xyz, uv in zip(
                ((-0.75, -0.6, 0), (-0.75, 0.6, 0), (0.75, 0.6, 0), (0.75, -0.6, 0)),
                ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
            ):
                p = g.createPoint()
                p.setPosition(xyz)
                p.setAttribValue("uv", uv)
                poly.addVertex(p)
            stash = obj.createNode("stash", "OUT")
            pass_fixture.parm(stash, "stash").set(g)
            stash.setDisplayFlag(True)
            stash.setRenderFlag(True)
            self.objects.append(obj)
        cam = hou.node("/obj").createNode("cam", "camera")
        pass_fixture.set_parameters(cam, dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "source_recovery")
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=" ".join(o.path() for o in self.objects),
                camera=cam.path(),
                resx=128,
                resy=64,
                samples=32,
                ipr_samples=32,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                pass_position=1,
                pass_normal=1,
                pass_uv=0,
            ),
        )
        self.rop.parmTuple("background").set((1, 1, 1))
        self.configure(CASES[0])

    def configure(self, case):
        self.case = case
        for node in self.material.children():
            if node.name() != "surface_output":
                node.destroy()
        output = self.material.node("surface_output")

        def create(kind, name):
            return self.material.createNode("cycles::" + kind + "::1", name)

        self.surface = create(
            "diffuse_bsdf" if case in ("normal_map", "normal_input") else "emission", "surface_shader"
        )
        self.surface.parmTuple("color").set((0.2, 0.6, 0.3))
        if pass_fixture.parm(self.surface, "strength"):
            pass_fixture.parm(self.surface, "strength").set(1)
        output.setNamedInput("surface", self.surface, self.surface.outputNames()[0])
        height = create("displacement", "height")
        pass_fixture.set_parameters(height, dict(height=0.25, midlevel=0, scale=1, space="object"))
        output.setNamedInput("displacement", height, "displacement")
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        kind = dict(
            attribute="attribute",
            shared_height="attribute",
            normal_input="attribute",
            color_layer="vertex_color",
            uv_map="uvmap",
            tangent="tangent",
            normal_map="normal_map",
            vector_displacement="vector_displacement",
        )[case]
        source = create(kind, "requested_attribute")
        self.source = source
        self.parm = pass_fixture.parm(source, "layer_name" if case == "color_layer" else "attribute")
        self.good = "Cd" if case in ("attribute", "shared_height", "color_layer", "normal_input") else "uv"
        self.parm.set(self.good)
        if case in ("attribute", "shared_height", "color_layer"):
            self.surface.setNamedInput("color", source, "color")
            if case == "shared_height":
                bw = create("rgb_to_bw", "height_from_color")
                bw.setNamedInput("color", source, "color")
                height.setNamedInput("height", bw, "val")
        elif case == "uv_map":
            self.surface.setNamedInput("color", source, "UV")
        elif case == "tangent":
            pass_fixture.parm(source, "direction_type").set("uv_map")
            self.surface.setNamedInput("color", source, "tangent")
        elif case == "normal_map":
            pass_fixture.parm(source, "space").set("tangent")
            source.parmTuple("color").set((0.8, 0.5, 1))
            self.surface.setNamedInput("normal", source, "normal")
        elif case == "normal_input":
            self.surface.setNamedInput("normal", source, "vector")
        elif case == "vector_displacement":
            pass_fixture.set_parameters(source, dict(space="tangent", midlevel=0, scale=1))
            source.parmTuple("vector").set((0, 0.25, 0))  # Cycles tangent displacement uses Y along the normal.
            output.setNamedInput("displacement", source, "displacement")
        self.material.layoutChildren()

    def check(self, frame, status, broken, baseline=None):
        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(64, 128, p["channels"])

        rgb = plane("combined")
        position = plane("position")
        normal = plane("normal")
        assert status["state"] != "failed" and not status["error"], status
        assert status["object_resources"] == status["geometry_resources"] == 2, status
        for p in (rgb, position, normal):
            assert np.isfinite(p).all()
        assert np.allclose(rgb[32, 32, :3], (0.2, 0.6, 0.3), atol=1e-5), rgb[32, 32]
        assert rgb[32, 96, 3] > 0.99, rgb[32, 96]
        issues = status["source_diagnostics"]
        assert bool(issues) == broken, issues
        if broken:
            assert all(d["code"] in ("material.unsupported_attribute", "material.missing_attribute") for d in issues), (
                issues
            )
            if self.case not in ("normal_map", "normal_input", "vector_displacement"):
                assert np.allclose(rgb[32, 96, :3], (1, 0, 1), atol=1e-5), rgb[32, 96]
            if self.case in ("normal_map", "normal_input"):
                assert np.allclose(normal[32, 96, :3], (0, 0, 1), atol=1e-5), normal[32, 96]
                assert rgb[32, 96, 1] > rgb[32, 96, 0], rgb[32, 96]
            height = 0 if self.case in ("shared_height", "vector_displacement") else 0.25
            assert abs(position[32, 96, 2] - height) < 1e-5, (self.case, position[32, 96])
        else:
            assert position[32, 96, 2] > 0.15, (self.case, position[32, 96])
            if self.case in ("normal_map", "normal_input"):
                assert abs(normal[32, 96, 0]) > 0.1 and normal[32, 96, 2] < 0.95, (self.case, normal[32, 96])
            if baseline:
                for name, p in (("combined", rgb), ("position", position), ("normal", normal)):
                    assert np.allclose(p, baseline[name], atol=1e-5), (self.case, name, "repair differs")
        return dict(combined=rgb.copy(), position=position.copy(), normal=normal.copy())
