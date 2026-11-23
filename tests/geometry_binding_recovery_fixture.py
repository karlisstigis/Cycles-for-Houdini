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
    "true_displacement",
    "both_displacement",
    "tangent_normal",
    "tangent_color",
    "tangent_vector_bump",
    "combined_volume",
    "volume_only_field",
    "mesh_grid_volume",
)


class Fixture:
    def __init__(self, device):
        self.dest = artifacts_root() / "examples/geometry-binding-recovery"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="shared_binding", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        self.obj = hou.node("/obj").createNode("geo", "mixed_bindings", run_init_scripts=False)
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.material.path())
        g = hou.Geometry()
        for name, value in (("pscale", 0.4), ("width", 0.4), ("uv", (0.5, 0.5, 0.0)), ("Cd", (0.2, 0.6, 0.3))):
            g.addAttrib(hou.attribType.Point, name, value)
        poly = g.createPolygon()
        for xyz, uv in zip(
            ((-2.4, -0.1, 0), (-2.4, 1.1, 0), (-1.2, 1.1, 0), (-1.2, -0.1, 0)),
            ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
        ):
            p = g.createPoint()
            p.setPosition(xyz)
            p.setAttribValue("uv", uv)
            poly.addVertex(p)
        g.createPoint().setPosition((0, 0.5, 0))
        curve = g.createPolygon(is_closed=False)
        for y in (-0.1, 1.1):
            p = g.createPoint()
            p.setPosition((1.8, y, 0))
            curve.addVertex(p)
        self.stash = self.obj.createNode("stash", "OUT")
        pass_fixture.parm(self.stash, "stash").set(g)
        self.stash.setDisplayFlag(True)
        self.stash.setRenderFlag(True)
        self.reference = ch.create_material(name="unchanged", shader="emission")
        self.reference.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
        pass_fixture.parm(self.reference.node("surface_shader"), "strength").set(1)
        neighbor = hou.node("/obj").createNode("geo", "unchanged", run_init_scripts=False)
        pass_fixture.parm(neighbor, "shop_materialpath").set(self.reference.path())
        box = neighbor.createNode("box")
        box.parmTuple("size").set((1, 0.5, 0.1))
        pass_fixture.parm(box, "ty").set(-1)
        box.setDisplayFlag(True)
        box.setRenderFlag(True)
        camera = hou.node("/obj").createNode("cam", "camera")
        pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=6))
        self.rop = hou.node("/out").createNode("cycles_ipr", "binding_recovery")
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=self.obj.path() + " " + neighbor.path(),
                camera=camera.path(),
                resx=192,
                resy=128,
                samples=64,
                ipr_samples=64,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                pass_position=1,
                pass_normal=1,
            ),
        )
        self.rop.parmTuple("background").set((1, 1, 1))
        self.configure(CASES[0])

    def configure(self, case):
        self.case = case
        for n in self.material.children():
            if n.name() != "surface_output":
                n.destroy()
        output = self.material.node("surface_output")
        output.setInput(1, None)
        output.setInput(2, None)

        def create(kind, name):
            return self.material.createNode("cycles::" + kind + "::1", name)

        surface = create("diffuse_bsdf" if case == "tangent_normal" else "emission", "surface")
        surface.parmTuple("color").set((0.2, 0.6, 0.3))
        if pass_fixture.parm(surface, "strength"):
            pass_fixture.parm(surface, "strength").set(1)
        output.setNamedInput("surface", surface, surface.outputNames()[0])
        pass_fixture.parm(self.material, "cycles_displacement_method").set("bump")
        self.source = surface
        callback = ""

        def wiring(socket, node, port):
            return (
                "o=hou.node(%r); n=hou.node(%r); o.setNamedInput(%r,n,%r) if kwargs['node'].evalParm('test_broken') else o.setInput(o.inputIndex(%r),None)"
                % (output.path(), node.path(), socket, port, socket)
            )

        if case in ("true_displacement", "both_displacement"):
            height = create("displacement", "height")
            pass_fixture.set_parameters(height, dict(height=0.25, midlevel=0, scale=1, space="object"))
            output.setNamedInput("displacement", height, "displacement")
            callback = (
                "hou.node(%r).parm('cycles_displacement_method').set(%r if kwargs['node'].evalParm('test_broken') else 'bump')"
                % (self.material.path(), "true" if case == "true_displacement" else "both")
            )
        elif case in ("tangent_normal", "tangent_color"):
            source = create("normal_map" if case == "tangent_normal" else "tangent", "mesh_tangents")
            if case == "tangent_normal":
                pass_fixture.parm(source, "space").set("tangent")
                source.parmTuple("color").set((0.8, 0.5, 1))
            else:
                pass_fixture.parm(source, "direction_type").set("uv_map")
            socket, port = ("normal", "normal") if case == "tangent_normal" else ("color", "tangent")
            callback = (
                "o=hou.node(%r); n=hou.node(%r); o.setNamedInput(%r,n,%r) if kwargs['node'].evalParm('test_broken') else o.setInput(o.inputIndex(%r),None)"
                % (surface.path(), source.path(), socket, port, socket)
            )
        elif case == "tangent_vector_bump":
            source = create("vector_displacement", "height")
            pass_fixture.set_parameters(source, dict(space="tangent", midlevel=0, scale=1))
            source.parmTuple("vector").set((0, 0.1, 0))
            callback = wiring("displacement", source, "displacement")
        else:
            volume = create("scatter_volume", "medium")
            pass_fixture.parm(volume, "density").set(0)
            if case in ("volume_only_field", "mesh_grid_volume"):
                field = create("attribute", "required_density")
                pass_fixture.parm(field, "attribute").set("density")
                volume.setNamedInput("density", field, "fac")
            callback = wiring("volume", volume, "volume")
            if case == "volume_only_field":
                callback += (
                    "; o.setInput(o.inputIndex('surface'),None) if kwargs['node'].evalParm('test_broken') else o.setNamedInput('surface',hou.node(%r),'emission')"
                    % surface.path()
                )
        pt = hou.IntParmTemplate("test_broken", "Unsupported Binding", 1, default_value=(0,), min=0, max=1)
        pt.setScriptCallback("import hou; " + callback)
        pt.setScriptCallbackLanguage(hou.scriptLanguage.Python)
        surface.addSpareParmTuple(pt)
        self.parm = pass_fixture.parm(surface, "test_broken")
        self.material.layoutChildren()

    def edit(self, broken):
        self.parm.set(int(broken))
        self.parm.pressButton()

    def check(self, frame, status, broken, baseline=None, legacy=False):
        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(128, 192, p["channels"])

        rgb = plane("combined")
        position = plane("position")
        normal = plane("normal")
        assert status["state"] != "failed" and not status["error"], status
        assert status["geometry_resources"] == status["object_resources"] == 4, status
        for p in (rgb, position, normal):
            assert np.isfinite(p).all()
        assert np.allclose(rgb[32, 96, :3], (0.1, 0.7, 0.2), atol=1e-5), rgb[32, 96]
        issues = status["source_diagnostics"]
        assert bool(issues) == broken, (self.case, issues)
        if broken:
            assert all(d["code"] == "material.geometry_binding" for d in issues), issues
            for x in (96, 154):
                expected = (1, 0, 1) if self.case in ("tangent_color", "volume_only_field") else (0.2, 0.6, 0.3)
                if self.case != "tangent_normal":
                    assert np.allclose(rgb[80, x, :3], expected, atol=1e-5), (self.case, x, rgb[80, x])
            if self.case == "volume_only_field" and legacy:
                # Implicit sparse-grid convenience kinds still recover on a mesh.
                assert np.allclose(rgb[80, 38, :3], (1, 0, 1), atol=1e-5), rgb[80, 38]
            elif self.case == "volume_only_field":
                # A mesh medium reading "density" renders as Blender's Cycles does (task L1): the
                # attribute is not found, the Attribute node returns 0, so the volume-only quad is an
                # empty medium and the background shows through; only the strands recover.
                assert np.allclose(rgb[80, 38, :3], rgb[4, 4, :3], atol=1e-4), (rgb[80, 38], rgb[4, 4])
            if self.case in ("true_displacement", "both_displacement"):
                assert abs(position[80, 38, 2] - 0.25) < 1e-5, position[80, 38]
            if baseline:
                for x in (96, 154):
                    assert np.allclose(position[80, x], baseline["position"][80, x], atol=1e-5), (
                        self.case,
                        "strand moved",
                        x,
                    )
                    if self.case == "tangent_normal":
                        assert np.allclose(normal[80, x], baseline["normal"][80, x], atol=1e-5), (
                            self.case,
                            x,
                            normal[80, x],
                        )
                if self.case == "tangent_normal":
                    assert abs(normal[80, 38, 0] - baseline["normal"][80, 38, 0]) > 0.2, normal[80, 38]
        elif baseline:
            for name, p in (("combined", rgb), ("position", position), ("normal", normal)):
                assert np.allclose(p, baseline[name], atol=1e-5), (self.case, name, "repair differs")
        return dict(combined=rgb.copy(), position=position.copy(), normal=normal.copy())
