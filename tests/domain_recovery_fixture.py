# SPDX-License-Identifier: Apache-2.0
"""Artist-editable source faults, with independent geometry and volume checks."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import os
from pathlib import Path
import subprocess
import hou
import numpy as np
import cycles_houdini as ch
from shader_source_recovery_fixture import Fixture as SurfaceFixture

CASES = (
    "surface_volume",
    "dupli_uv",
    "dupli_generated",
    "reference_object",
    "dupli_uv_shared_height",
    "uvmap_instancer",
    "unused_coordinate_switch",
    "volume_uvmap",
    "volume_tangent",
    "volume_normal",
    "volume_sky",
    "volume_image",
    "volume_pointiness",
    "volume_bsdf",
    "volume_color",
    "volume_uv",
    "volume_dupli",
    "volume_empty_field",
    "volume_surface_binding",
)


class Fixture(SurfaceFixture):
    def __init__(self, device):
        super().__init__(device)
        self.dest = artifacts_root() / "examples/domain-recovery"
        self.dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [str(runtime_executable("ch_harness.exe")), "--write-volume-motion-fixtures", str(self.dest)], check=True
        )
        self.volume_obj = hou.node("/obj").createNode("geo", "domain_volume", run_init_scripts=False)
        source = self.volume_obj.createNode("file")
        source.parm("file").set(str(self.dest / "vector.vdb"))
        g = hou.Geometry()
        g.merge(source.geometry())
        g.deletePrims([p for p in g.prims() if p.attribValue("name") != "density"], keep_points=False)
        stash = self.volume_obj.createNode("stash")
        stash.parm("stash").set(g)
        source.destroy()
        stash.setDisplayFlag(True)
        stash.setRenderFlag(True)
        self.volume_obj.parm("tx").set(1)
        self.volume_material = ch.create_material(name="domain_medium", shader="absorption_volume")
        self.volume_obj.parm("shop_materialpath").set(self.volume_material.path())
        # Full-frame before/repair parity requires identical sample counts.
        # Adaptive convergence can flip an edge pixel's stopping decision from
        # floating-point accumulation order on OptiX, even in healthy renders.
        self.rop.setParms(dict(samples=256, ipr_samples=256, adaptive_sampling=0, ipr_adaptive_sampling=0))
        self.configure(CASES[0])

    def configure(self, case):
        if case == "attribute":
            return super().configure(case)  # base setup
        self.case = case
        self.is_volume = case.startswith("volume_")
        material = self.volume_material if self.is_volume else self.material
        for node in material.children():
            if node.name() not in ("surface_output", "volume_output"):
                node.destroy()

        def create(kind, name):
            return material.createNode("cycles::" + kind + "::1", name)

        output = material.node("volume_output" if self.is_volume else "surface_output")
        terminal = "volume" if self.is_volume else "surface"
        healthy = create("absorption_volume" if self.is_volume else "emission", "healthy")
        if self.is_volume:
            healthy.parmTuple("color").set((0, 0, 0))
            density = create("attribute", "density")
            density.parm("attribute").set("density")
            healthy.setNamedInput("density", density, "fac")
        else:
            healthy.parmTuple("color").set((0.2, 0.6, 0.3))
            healthy.parm("strength").set(1)
            height = create("displacement", "height")
            height.setParms(dict(height=0.25, midlevel=0, scale=1, space="object"))
            output.setNamedInput("displacement", height, "displacement")
            material.parm("cycles_displacement_method").set("true")
        output.setNamedInput(terminal, healthy, "volume" if self.is_volume else "emission")
        control = healthy
        callback = ""
        if case in ("surface_volume", "surface_background", "volume_bsdf"):
            bad = create(
                {
                    "surface_volume": "scatter_volume",
                    "surface_background": "background_shader",
                    "volume_bsdf": "diffuse_bsdf",
                }[case],
                "wrong_domain",
            )
            bad_socket = bad.outputNames()[0]
            callback = (
                "o=hou.node(%r); n=hou.node(%r if kwargs['node'].evalParm('test_broken') else %r); o.setNamedInput(%r,n,%r if kwargs['node'].evalParm('test_broken') else %r)"
                % (
                    output.path(),
                    bad.path(),
                    healthy.path(),
                    terminal,
                    bad_socket,
                    "volume" if self.is_volume else "emission",
                )
            )
        elif case == "volume_surface_binding":
            callback = (
                "hou.node(%r).parm('shop_materialpath').set(%r if kwargs['node'].evalParm('test_broken') else %r)"
                % (self.volume_obj.path(), self.material.path(), material.path())
            )
        elif self.is_volume:
            bad = create(
                {
                    "volume_color": "vertex_color",
                    "volume_uv": "texture_coordinate",
                    "volume_dupli": "texture_coordinate",
                    "volume_empty_field": "attribute",
                    "volume_uvmap": "uvmap",
                    "volume_tangent": "tangent",
                    "volume_normal": "normal_map",
                    "volume_sky": "sky_texture",
                    "volume_image": "image_texture",
                    "volume_pointiness": "geometry",
                }[case],
                "bad_source",
            )
            if case == "volume_empty_field":
                bad.parm("attribute").set("")
            if case == "volume_tangent":
                bad.parm("direction_type").set("uv_map")
            if case == "volume_normal":
                bad.parm("space").set("tangent")
            if case == "volume_dupli":
                bad.parm("from_dupli").set(1)
            socket = {
                "volume_color": "color",
                "volume_uv": "UV",
                "volume_dupli": "generated",
                "volume_empty_field": "fac",
                "volume_uvmap": "UV",
                "volume_tangent": "tangent",
                "volume_normal": "normal",
                "volume_sky": "color",
                "volume_image": "color",
                "volume_pointiness": "pointiness",
            }[case]
            callback = (
                "o=hou.node(%r); n=hou.node(%r if kwargs['node'].evalParm('test_broken') else %r); o.setNamedInput('density',n,%r if kwargs['node'].evalParm('test_broken') else 'fac')"
                % (healthy.path(), bad.path(), density.path(), socket)
            )
        else:
            coord = create("uvmap" if case == "uvmap_instancer" else "texture_coordinate", "coordinates")
            control = coord
            socket = (
                "generated"
                if case == "dupli_generated"
                else "object"
                if case in ("reference_object", "unused_coordinate_switch")
                else "UV"
            )
            healthy.setNamedInput("color", coord, socket)
            if case == "dupli_uv_shared_height":
                bw = create("rgb_to_bw", "height_input")
                bw.setNamedInput("color", coord, "UV")
                height.setNamedInput("height", bw, "val")
            elif case == "dupli_uv":
                # The same node's unaffected Normal output remains displacement.
                output.setNamedInput("displacement", coord, "normal")
            flag = "use_transform" if case == "reference_object" else "from_dupli"
            coord.parm(flag).setExpression('ch("test_broken")')
        pt = hou.IntParmTemplate("test_broken", "Broken Input", 1, default_value=(0,), min=0, max=1)
        if callback:
            pt.setScriptCallback("import hou; " + callback)
            pt.setScriptCallbackLanguage(hou.scriptLanguage.Python)
        control.addSpareParmTuple(pt)
        self.parm = control.parm("test_broken")
        self.source = control
        self.volume_obj.parm("shop_materialpath").set(self.volume_material.path())
        self.rop.parm("vobject").set(
            self.objects[0].path() + " " + (self.volume_obj if self.is_volume else self.objects[1]).path()
        )
        material.layoutChildren()

    def edit(self, broken):
        self.parm.set(int(broken))
        if self.parm.parmTemplate().scriptCallback():
            self.parm.pressButton()

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
        issues = status["source_diagnostics"]
        warn = broken and self.case != "unused_coordinate_switch"
        assert bool(issues) == warn, (self.case, issues)
        if self.is_volume:
            center = rgb[29:35, 93:99, :3].mean(axis=(0, 1))
            if broken:
                assert center[0] > center[1] + 0.05 and center[2] > center[1] + 0.05, (self.case, center)
            else:
                assert np.max(np.abs(center - np.exp(-1.125))) < 0.065, (self.case, center)
        else:
            assert rgb[32, 96, 3] > 0.99, rgb[32, 96]
            if warn:
                assert np.allclose(rgb[32, 96, :3], (1, 0, 1), atol=1e-5), (self.case, rgb[32, 96])
            if self.case == "dupli_uv_shared_height":
                if broken:
                    assert abs(position[32, 96, 2]) < 1e-5, position[32, 96]
                else:
                    assert position[32, 96, 2] > 0.1, position[32, 96]
            else:
                expected = 1 if self.case == "dupli_uv" else 0.25
                assert abs(position[32, 96, 2] - expected) < 1e-5, (self.case, position[32, 96])
        if not broken and baseline:
            for name, p in (("combined", rgb), ("position", position), ("normal", normal)):
                assert np.allclose(p, baseline[name], atol=1e-5), (self.case, name, "repair differs")
        return dict(combined=rgb.copy(), position=position.copy(), normal=normal.copy())
