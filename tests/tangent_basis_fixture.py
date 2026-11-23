# SPDX-License-Identifier: Apache-2.0
"""Native grids and UV sets; no Python SOP, manual IDs or Blender data blocks."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

CASES = {
    "tangent3": ("tangent", 0, 1, 0, 0, 0, 0),
    "tangent2": ("tangent", 1, 1, 0, 0, 0, 0),
    "tangent_uv": ("tangent", 0, 1, 0, 0, 48, 0),
    "tangent_geometry": ("tangent", 0, 1, 0, 0, 0, 24),
    "tangent_both": ("tangent", 1, 1, 0, 1, 48, 24),
    "normal_original": ("normal", 0, 1, 0, 0, 48, 0),
    "normal_displaced": ("normal", 1, 1, 0, 1, 48, 24),
    "normal_mirrored": ("normal", 0, -1, 1, 0, 48, 0),
    "normal_tilt": ("normal", 0, 1, 0, 1, 0, 24),
    "displacement_uv": ("displacement", 0, 1, 0, 0, 48, 0),
    "displacement_geometry": ("displacement", 0, 1, 0, 1, 0, 24),
    "displacement_both": ("displacement", 1, -1, 0, 1, 48, 24),
    "tangent_displacement": ("tangent_displacement", 0, 1, 0, 1, 48, 24),
    "normal_to_displacement": ("normal_displacement", 1, 1, 0, 1, 48, 24),
    "normal_displaced_to_displacement": ("normal_displacement", 0, 1, 0, 1, 48, 24),
    "shared_normal_displaced": ("shared_normal", 1, 1, 0, 1, 48, 24),
}


class Fixture:
    width, height = 192, 96

    def __init__(self, device):
        hou.setFps(24)
        hou.setFrame(1)
        self.dest = artifacts_root() / "examples/tangent-bases"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="named_uv_basis", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        uniform = ch.create_material(name="unaffected", shader="emission")
        uniform.node("surface_shader").parmTuple("color").set((0.2, 0.6, 0.3))
        pass_fixture.parm(uniform.node("surface_shader"), "strength").set(1)
        self.objects = []
        for i, x in enumerate((-1, 1)):
            obj = hou.node("/obj").createNode(
                "geo", "unaffected" if not i else "named_uv_receiver", run_init_scripts=False
            )
            pass_fixture.set_parameters(obj, dict(tx=x, shop_materialpath=(uniform if not i else self.material).path()))
            grid = obj.createNode("grid", "base_grid")
            pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
            grid.parmTuple("size").set((1.2, 1))
            # Houdini's XY Grid faces -Z. Match the independently specified +Z
            # winding and authored normals, rather than testing a backface.
            reverse = obj.createNode("reverse", "front_facing_grid")
            reverse.setInput(0, grid)
            reverse.setDisplayFlag(True)
            reverse.setRenderFlag(True)
            self.objects.append(obj)
        self.obj = self.objects[1]
        self.animate = self.obj.createNode("attribwrangle", "UV_and_geometry_animation")
        self.animate.setInput(0, self.obj.node("front_facing_grid"))
        group = self.animate.parmTemplateGroup()
        for name, label, default in (
            ("uv_speed", "UV angular speed", 0.0),
            ("geometry_speed", "Geometry angular speed", 0.0),
            ("mirror", "UV handedness", 1.0),
            ("tilt", "Tilt geometry instead of twisting", 0.0),
        ):
            group.append(hou.FloatParmTemplate(name, label, 1, default_value=(default,)))
        group.append(
            hou.IntParmTemplate(
                "test_broken",
                "UV source: valid / missing / wrong type / nonfinite",
                1,
                default_value=(0,),
                min=0,
                max=3,
            )
        )
        group.append(
            hou.ToggleParmTemplate("test_shutter_only", "Break only the first half of the shutter", default_value=False)
        )
        self.animate.setParmTemplateGroup(group)
        self.control = pass_fixture.parm(self.animate, "test_broken")
        self.shutter_only = pass_fixture.parm(self.animate, "test_shutter_only")
        pass_fixture.parm(
            self.animate, "snippet"
        ).set("""vector p=@P; float a=.35+@Time*chf("uv_speed"), b=a+1.5707963267948966, phi=@Time*chf("geometry_speed"), m=chf("mirror");
v@named_uv3=set(cos(a)*p.x+sin(a)*p.y+.5,m*(-sin(a)*p.x+cos(a)*p.y)+.5,23+p.x);
u@named_uv2=set(cos(b)*p.x+sin(b)*p.y+.5,m*(-sin(b)*p.x+cos(b)*p.y)+.5);
v@uv=set(p.x+.5,p.y+.5,0);
if(chf("tilt")) { @P=set(cos(phi)*p.x,p.y,-sin(phi)*p.x); v@N=set(sin(phi),0,cos(phi)); }
else { @P=set(cos(phi)*p.x-sin(phi)*p.y,sin(phi)*p.x+cos(phi)*p.y,0); v@N=set(0,0,1); }""")
        self.base_snippet = pass_fixture.parm(self.animate, "snippet").eval()
        self.remove_default = self.obj.createNode("attribdelete", "remove_unneeded_default_uv")
        self.remove_default.setInput(0, self.animate)
        pass_fixture.parm(self.remove_default, "ptdel").set("uv")
        self.missing = self.obj.createNode("attribdelete", "remove_required_map")
        self.missing.setInput(0, self.remove_default)
        self.invalid = self.obj.createNode("attribwrangle", "wrong_map_type")
        self.invalid.setInput(0, self.missing)
        self.nonfinite = self.obj.createNode("attribwrangle", "nonfinite_map")
        self.nonfinite.setInput(0, self.remove_default)
        self.switch = self.obj.createNode("switch", "OUT_NAMED_UV")
        for n, node in enumerate((self.remove_default, self.missing, self.invalid, self.nonfinite)):
            self.switch.setInput(n, node)
        pass_fixture.parm(self.switch, "input").setExpression(
            'if(ch("%s") && $T>=0,0,ch("%s"))' % (self.shutter_only.path(), self.control.path()),
            hou.exprLanguage.Hscript,
        )
        self.switch.setDisplayFlag(True)
        self.switch.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "basis_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=4.6, shutter=0.5))
        self.rop = hou.node("/out").createNode("cycles_ipr", "tangent_bases")
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=" ".join(o.path() for o in self.objects),
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=1024,
                ipr_samples=1024,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                pass_position=1,
                pass_normal=1,
                pass_uv=0,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                film_exposure=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.configure("tangent3")

    def configure(self, case):
        self.case = case
        kind, map2, mirror, directx, tilt, uv_speed, geo_speed = CASES[case]
        self.field = "named_uv2" if map2 else "named_uv3"
        self.mode = (
            "tangent"
            if kind.startswith("tangent")
            else "normal"
            if kind.startswith("normal") or kind == "shared_normal"
            else "displacement"
        )
        self.displacement_driven = kind in (
            "displacement",
            "tangent_displacement",
            "normal_displacement",
            "shared_normal",
        )
        self.visualized = kind in ("tangent", "normal", "shared_normal")
        pass_fixture.parm(self.animate, "snippet").set(self.base_snippet)
        pass_fixture.parm(self.remove_default, "ptdel").set("uv")
        self.control.set(0)
        self.shutter_only.set(False)
        pass_fixture.set_parameters(
            self.animate, dict(uv_speed=uv_speed, geometry_speed=geo_speed, mirror=mirror, tilt=tilt)
        )
        for node in self.material.children():
            if node.name() != "surface_output":
                node.destroy()

        def create(kind, name):
            return self.material.createNode("cycles::" + kind + "::1", name)

        output = self.material.node("surface_output")
        surface = create("emission", "surface_shader")
        pass_fixture.parm(surface, "strength").set(1)
        surface.parmTuple("color").set((0.2, 0.6, 0.3))
        output.setNamedInput("surface", surface, "emission")
        self.surface = surface
        self.source = create(
            "tangent" if self.mode == "tangent" else "normal_map" if self.mode == "normal" else "vector_displacement",
            "required_uv_map",
        )
        pass_fixture.parm(self.source, "attribute").set(self.field)
        self.map_parm = pass_fixture.parm(self.source, "attribute")
        if self.mode == "tangent":
            pass_fixture.parm(self.source, "direction_type").set("uv_map")
            socket = "tangent"
        elif self.mode == "normal":
            pass_fixture.set_parameters(
                self.source,
                dict(
                    space="tangent",
                    base="displaced"
                    if case in ("normal_displaced", "normal_displaced_to_displacement", "shared_normal_displaced")
                    else "original",
                    convention="directx" if directx else "opengl",
                ),
            )
            self.source.parmTuple("color").set((0.75, 0.6, 0.9))
            socket = "normal"
        else:
            pass_fixture.set_parameters(self.source, dict(space="tangent", midlevel=0, scale=1))
            self.source.parmTuple("vector").set((0.3, 0.12, 0.15))
            socket = "displacement"
        if self.visualized:
            half = create("vector_math", "half_vector")
            pass_fixture.parm(half, "math_type").set("multiply")
            half.parmTuple("vector2").set((0.5, 0.5, 0.5))
            half.setNamedInput("vector1", self.source, socket)
            offset = create("vector_math", "display_vector")
            pass_fixture.parm(offset, "math_type").set("add")
            offset.parmTuple("vector2").set((0.5, 0.5, 0.5))
            offset.setNamedInput("vector1", half, "vector")
            surface.setNamedInput("color", offset, "vector")
        if not self.displacement_driven:
            displacement = create("vector_displacement", "independent_displacement")
            pass_fixture.set_parameters(displacement, dict(space="object", midlevel=0, scale=1))
            displacement.parmTuple("vector").set((0, 0, 0.15))
        elif self.mode == "displacement":
            displacement = self.source
        else:
            displacement = create("vector_displacement", "offset_from_basis")
            pass_fixture.set_parameters(
                displacement, dict(space="world", midlevel=0, scale=0.3 if self.mode == "tangent" else 0.25)
            )
            displacement.setNamedInput("vector", self.source, socket)
        output.setNamedInput("displacement", displacement, "displacement")
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        pass_fixture.parm(self.missing, "ptdel").set(self.field)
        pass_fixture.parm(self.invalid, "snippet").set("f@%s=0;" % self.field)
        pass_fixture.parm(self.nonfinite, "snippet").set(
            ('u@%s=set(atof("nan"),0);' if map2 else 'v@%s=set(atof("nan"),0,0);') % self.field
        )
        self.material.layoutChildren()
        self.parm = self.control

    def edit(self, value):
        self.control.set(int(value))

    def authoring_controls(self):
        probe = ch.create_material(name="uv_control_probe")
        checks = []
        try:
            for kind in ("tangent", "normal_map", "vector_displacement", "uvmap"):
                node = probe.createNode("cycles::" + kind + "::1", "control_" + kind)
                assert pass_fixture.parm(node, "attribute").parmTemplate().label() == "UV Map"
                assert pass_fixture.parm(node, "context_tangent_bases") is None, (
                    "Internal context mask leaked into authoring"
                )
                pass_fixture.parm(node, "attribute").set("saved_map")
                states = (
                    (("direction_type", "radial", True), ("direction_type", "uv_map", False))
                    if kind == "tangent"
                    else (("space", "object", True), ("space", "world", True), ("space", "tangent", False))
                    if kind != "uvmap"
                    else ()
                )
                for parameter, value, disabled in states:
                    pass_fixture.parm(node, parameter).set(value)
                    node.updateParmStates()
                    # Inactive modes now hide their parameters and sockets;
                    # wired active inputs are independently disabled.
                    assert pass_fixture.parm(node, "attribute").isHidden() == disabled, (kind, value, disabled)
                    if kind == "normal_map":
                        assert pass_fixture.parm(node, "base").isHidden() == disabled
                    if kind == "tangent":
                        assert pass_fixture.parm(node, "axis").isHidden() == (not disabled)
                    assert pass_fixture.evaluate(node, "attribute") == "saved_map"
                    checks.append(dict(node=kind, mode=value, uv_map_hidden=disabled))
        finally:
            probe.destroy()
        return checks

    def planes(self, frame):
        return {
            name: np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"]).copy()
            for name, p in frame["planes"].items()
        }

    def check(self, frame, status, broken=False, baseline=None):
        assert not status["error"] and status["state"] != "failed", status
        assert status["object_resources"] == status["geometry_resources"] == 2, status
        p = self.planes(frame)
        assert all(np.isfinite(v).all() for v in p.values())
        assert np.allclose(p["combined"][48, 54, :3], (0.2, 0.6, 0.3), atol=1e-5), p["combined"][48, 54]
        visible = (p["position"][:, :, 0] > 0.2) & (np.linalg.norm(p["normal"], axis=2) > 0.5)
        assert visible.sum() > 30, "Affected mesh disappeared"
        issues = status["source_diagnostics"]
        assert bool(issues) == bool(broken), (self.case, broken, issues)
        if broken:
            assert any(d["attribute"] == self.field for d in issues), issues
        if baseline is not None:
            difference = float(np.abs(p["combined"] - baseline["combined"]).mean())
            changes = bool(broken) and (
                not self.shutter_only.eval() or pass_fixture.evaluate(self.animate, "uv_speed") != 0
            )
            assert difference > 0.0001 if changes else difference < 1e-6, (self.case, broken, difference)
        return p
