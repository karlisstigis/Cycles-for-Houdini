from support.paths import artifacts_root
import pass_fixtures as pass_fixture

# SPDX-License-Identifier: Apache-2.0
from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

CASES = ("deformation", "transform", "combined", "independent_transform")


class Fixture:
    def __init__(self, device):
        hou.setFps(24)
        hou.setFrame(1)
        self.dest = artifacts_root() / "examples/displacement-motion-recovery"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="displaced", shader="emission")
        self.material.node("surface_shader").parmTuple("color").set((0.2, 0.6, 0.3))
        pass_fixture.parm(self.material.node("surface_shader"), "strength").set(1)
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        self.vector = self.material.createNode("cycles::vector_displacement::1", "offset")
        pass_fixture.set_parameters(self.vector, dict(midlevel=0, scale=1, space="object"))
        self.vector.parmTuple("vector").set((0, 0, 0.25))
        self.scalar = self.material.createNode("cycles::displacement::1", "normal_offset")
        pass_fixture.set_parameters(self.scalar, dict(height=0.25, midlevel=0, scale=1, space="object"))
        # Missing off-frame UVs remain a real recovery case now that valid
        # shutter UV/Cd sampling is supported. The transform-only packed case
        # also exercises a supported identity basis without dropping motion.
        self.attribute = self.material.createNode("cycles::attribute::1", "pending_sampled_attribute")
        pass_fixture.parm(self.attribute, "attribute").set("Cd")
        self.pending_basis = self.material.createNode("cycles::vector_transform::1", "pending_sampled_basis")
        pass_fixture.set_parameters(
            self.pending_basis, dict(transform_type="vector", convert_from="object", convert_to="object")
        )
        self.pending_basis.setNamedInput("vector", self.attribute, "color")
        self.recovery_uv = self.material.createNode("cycles::uvmap::1", "shutter_uv")
        self.recovery_u = self.material.createNode("cycles::separate_xyz::1", "shutter_u")
        self.recovery_u.setNamedInput("vector", self.recovery_uv, "UV")
        self.scalar.setNamedInput("height", self.recovery_u, "x")
        self.attribute_vector = self.material.createNode("cycles::vector_math::1", "pending_attribute_offset")
        pass_fixture.parm(self.attribute_vector, "math_type").set("multiply")
        self.attribute_vector.parmTuple("vector2").set((0, 0, 1))
        self.attribute_vector.setNamedInput("vector1", self.pending_basis, "vector")
        self.source = self.material.node("surface_shader")
        self.source.addSpareParmTuple(
            hou.IntParmTemplate("test_broken", "Unsupported Shutter Displacement", 1, default_value=(0,), min=0, max=1)
        )
        self.parm = pass_fixture.parm(self.source, "test_broken")
        self.obj, self.sop = self.make_plane("affected", self.material, -1, 0)
        self.static, _ = self.make_plane("shared_static", self.material, 1, 1)
        other = ch.create_material(name="independent", shader="emission")
        other.node("surface_shader").parmTuple("color").set((0.1, 0.2, 0.8))
        pass_fixture.parm(other.node("surface_shader"), "strength").set(1)
        self.other, _ = self.make_plane("independent_motion", other, 1, -1)
        self.missing_uv = self.obj.createNode("attribdelete", "missing_uv")
        self.missing_uv.setInput(0, self.sop)
        pass_fixture.parm(self.missing_uv, "ptdel").set("uv")
        self.recovery_switch = self.obj.createNode("switch", "OUT_SHUTTER_RECOVERY")
        self.recovery_switch.setInput(0, self.sop)
        self.recovery_switch.setInput(1, self.missing_uv)
        self.recovery_switch.setDisplayFlag(True)
        self.recovery_switch.setRenderFlag(True)
        pass_fixture.parm(self.other, "tx").setExpression("1+$T*48", hou.exprLanguage.Hscript)
        camera = hou.node("/obj").createNode("cam", "camera")
        pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=6, shutter=0.5))
        self.rop = hou.node("/out").createNode("cycles_ipr", "displacement_motion_recovery")
        # The default this test was written for (before the ROP took Blender's defaults).
        self.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=" ".join(n.path() for n in (self.obj, self.static, self.other)),
                camera=camera.path(),
                resx=192,
                resy=128,
                samples=128,
                ipr_samples=128,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                pass_position=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.configure(CASES[0])

    def make_plane(self, name, material, x, y):
        obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
        pass_fixture.set_parameters(obj, dict(tx=x, ty=y, shop_materialpath=material.path()))
        grid = obj.createNode("grid")
        pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
        grid.parmTuple("size").set((0.6, 0.6))
        reverse = obj.createNode("reverse")
        reverse.setInput(0, grid)
        color = obj.createNode("color")
        color.setInput(0, reverse)
        color.parmTuple("color").set((0.25, 0.25, 0.25))
        uv = obj.createNode("attribwrangle", "constant_uv")
        uv.setInput(0, color)
        pass_fixture.parm(uv, "snippet").set("v@uv=set(.25,0,0);")
        sop = obj.createNode("xform", "OUT")
        sop.setInput(0, uv)
        sop.setDisplayFlag(True)
        sop.setRenderFlag(True)
        return obj, sop

    def configure(self, case):
        self.case = case
        self.basis_recovery = False
        self.pending_basis.setNamedInput("vector", self.attribute, "color")
        self.scalar.setNamedInput("height", self.recovery_u, "x")
        pass_fixture.parm(self.recovery_switch, "input").setExpression(
            "0"
            if case == "transform"
            else 'if(abs($T)>1e-8 && $T<ch("%s")/96-1e-8,ch("%s"),0)'
            % (pass_fixture.parm(self.rop, "shutteroffset").path(), self.parm.path()),
            hou.exprLanguage.Hscript,
        )
        for p in (pass_fixture.parm(self.sop, "tx"), pass_fixture.parm(self.obj, "tx")):
            p.deleteAllKeyframes()
        sop_rate = 48 if case == "deformation" else 24 if case in ("combined", "independent_transform") else 0
        obj_rate = 48 if case == "transform" else 24 if case in ("combined", "independent_transform") else 0
        pass_fixture.parm(self.sop, "tx").setExpression("$T*%d" % sop_rate, hou.exprLanguage.Hscript)
        pass_fixture.parm(self.obj, "tx").setExpression("-1+$T*%d" % obj_rate, hou.exprLanguage.Hscript)
        pass_fixture.parm(self.vector, "space").set("object")
        pass_fixture.parm(self.scalar, "space").set("world" if case == "combined" else "object")
        self.vector.setInput(self.vector.inputIndex("vector"), None)
        output = self.material.node("surface_output")
        if case == "transform":
            self.vector.setNamedInput("vector", self.attribute_vector, "vector")
            self.attribute_vector.setNamedInput("vector1", self.attribute, "color")
            callback = (
                "broken=bool(kwargs['node'].evalParm('test_broken')); hou.node(%r).parm('space').set('world' if broken else 'object'); "
                "hou.node(%r).setNamedInput('vector1',hou.node(%r if broken else %r),'vector' if broken else 'color')"
            ) % (self.vector.path(), self.attribute_vector.path(), self.pending_basis.path(), self.attribute.path())
            output.setNamedInput("displacement", self.vector, "displacement")
        else:
            callback = (
                "hou.node(%r).setNamedInput('displacement',hou.node(%r if kwargs['node'].evalParm('test_broken') else %r),'displacement')"
                % (output.path(), self.scalar.path(), self.vector.path())
            )
        group = self.source.parmTemplateGroup()
        pt = group.find("test_broken")
        pt.setScriptCallback("import hou; " + callback)
        pt.setScriptCallbackLanguage(hou.scriptLanguage.Python)
        group.replace("test_broken", pt)
        self.source.setParmTemplateGroup(group)
        self.parm = pass_fixture.parm(self.source, "test_broken")
        self.edit(False)

    def edit(self, broken):
        self.parm.set(int(broken))
        self.parm.pressButton()

    def shader_only_recovery(self):
        self.basis_recovery = True
        pass_fixture.parm(self.recovery_switch, "input").deleteAllKeyframes()
        pass_fixture.parm(self.recovery_switch, "input").set(0)
        self.scalar.setNamedInput("height", self.pending_basis, "vector")

    def check(self, frame, status, broken, baseline=None):
        p = frame["planes"]["combined"]
        rgb = np.frombuffer(p["pixels"], np.float32).reshape(128, 192, p["channels"])
        p = frame["planes"]["position"]
        position = np.frombuffer(p["pixels"], np.float32).reshape(128, 192, p["channels"])
        assert status["state"] != "failed" and not status["error"], status
        assert status["geometry_resources"] == status["object_resources"] == 3, status
        assert np.isfinite(rgb).all() and np.isfinite(position).all()
        issues = status["source_diagnostics"]
        motion_issues = [d for d in issues if d["code"].startswith("motion.")]
        assert not motion_issues, (self.case, issues)
        assert all(d["code"].startswith("motion.") or d["code"] == "geometry.shutter_uv" for d in issues), issues
        width = int((rgb[64, :110, 1] > 0.005).sum())
        assert width > 40, (self.case, width)
        # Compatible shared binding still displaces; unrelated object stays blurred.
        assert abs(float(position[96, 128, 2]) - 0.25) < 1e-5, position[96, 128]
        visible = (rgb[64, :110, 1] > 0.005) & (position[64, :110, 2] > 0.1)
        assert visible.sum() > 10 and np.allclose(position[64, :110, 2][visible], 0.25, atol=1e-5), (
            self.case,
            "displaced depth differs",
        )
        assert (rgb[32, :, 2] > 0.005).sum() > 40
        if baseline is not None:
            assert np.allclose(rgb[:48], baseline["combined"][:48], atol=1e-5), "Independent motion changed"
            if not broken:
                assert np.allclose(rgb, baseline["combined"], atol=1e-5), "Repair differs"
        return dict(combined=rgb.copy(), position=position.copy())
