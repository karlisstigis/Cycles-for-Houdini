# SPDX-License-Identifier: Apache-2.0
"""Ordinary Houdini numeric fields, independent expected colors and local repairs."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

CASES = (
    "vector",
    "scalar",
    "integer",
    "float2",
    "color_layer",
    "uv_map",
    "detail",
    "primitive",
    "rest",
    "uv_attribute",
)


class Fixture:
    width, height = 144, 72

    def __init__(self, device):
        self.dest = artifacts_root() / "examples/named-attributes"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="named_fields", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        self.material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        self.objects = []
        self.stashes = []
        self.deletes = []
        for kind, x in enumerate((-1.6, 0, 1.6)):
            obj = hou.node("/obj").createNode(
                "geo", ("mesh_fields", "point_fields", "curve_fields")[kind], run_init_scripts=False
            )
            pass_fixture.set_parameters(obj, dict(tx=x, shop_materialpath=self.material.path()))
            g = hou.Geometry()
            for name, value in (
                ("paint", (0.2, 0.6, 0.3)),
                ("mask", 0.35),
                ("counter", 1),
                ("map2", (0.2, 0.6)),
                ("uv", (0.2, 0.6, 0.0)),
                ("uv2", (0.2, 0.6, 0.0)),
                ("rest", (0.0, 0.0, 0.0)),
            ):
                g.addAttrib(hou.attribType.Point, name, value)
            g.addAttrib(hou.attribType.Global, "gain", 0.45)
            g.addAttrib(hou.attribType.Point, "not_numeric", "ordinary unused string")
            g.addAttrib(hou.attribType.Point, "pscale", 0.14)
            g.addAttrib(hou.attribType.Point, "width", 0.16)
            g.addAttrib(hou.attribType.Point if kind == 1 else hou.attribType.Prim, "face_tint", (0.2, 0.6, 0.3))
            coords = (
                ((-0.6, -0.65, 0), (-0.6, 0.65, 0), (0.6, 0.65, 0), (0.6, -0.65, 0))
                if kind == 0
                else (
                    ((-0.25, -0.45, 0), (0.25, -0.45, 0), (0, 0, 0), (-0.25, 0.45, 0), (0.25, 0.45, 0))
                    if kind == 1
                    else ((-0.35, -0.65, 0), (-0.15, -0.22, 0), (0.15, 0.22, 0), (0.35, 0.65, 0))
                )
            )
            poly = g.createPolygon() if kind != 1 else None
            if kind == 2:
                poly.setIsClosed(False)
            for xyz in coords:
                p = g.createPoint()
                p.setPosition(xyz)
                p.setAttribValue("rest", tuple(0.5 + 0.2 * v for v in xyz))
                if poly:
                    poly.addVertex(p)
            stash = obj.createNode("stash", "authored_fields")
            pass_fixture.parm(stash, "stash").set(g)
            delete = obj.createNode("attribdelete", "remove_field")
            delete.setInput(0, stash)
            delete.setDisplayFlag(True)
            delete.setRenderFlag(True)
            self.objects.append(obj)
            self.stashes.append(stash)
            self.deletes.append(delete)
        self.camera = hou.node("/obj").createNode("cam", "fields_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=5.2))
        self.rop = hou.node("/out").createNode("cycles_ipr", "named_attributes")
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=" ".join(o.path() for o in self.objects),
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=32,
                ipr_samples=32,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                pass_position=1,
                pass_normal=1,
                pass_uv=0,
                transparent_background=1,
            ),
        )
        self.configure("vector")

    def configure(self, case):
        self.case = case
        for d in self.deletes:
            pass_fixture.parm(d, "ptdel").set("")
            pass_fixture.parm(d, "vtxdel").set("")
            pass_fixture.parm(d, "primdel").set("")
            pass_fixture.parm(d, "dtldel").set("")
        for n in self.material.children():
            if n.name() != "surface_output":
                n.destroy()
        self.surface = self.material.createNode("cycles::emission::1", "surface_shader")
        pass_fixture.parm(self.surface, "strength").set(1)
        self.material.node("surface_output").setNamedInput("surface", self.surface, "emission")
        kind = "vertex_color" if case == "color_layer" else "uvmap" if case == "uv_map" else "attribute"
        self.source = self.material.createNode("cycles::" + kind + "::1", "field")
        self.field = dict(
            vector="paint",
            scalar="mask",
            integer="counter",
            float2="map2",
            color_layer="paint",
            uv_map="uv2",
            detail="gain",
            primitive="face_tint",
            rest="rest",
            uv_attribute="uv",
        )[case]
        pass_fixture.parm(self.source, "layer_name" if kind == "vertex_color" else "attribute").set(self.field)
        self.surface.setNamedInput("color", self.source, "UV" if kind == "uvmap" else "color")
        self.expected = dict(
            vector=(0.2, 0.6, 0.3),
            scalar=(0.35,) * 3,
            integer=(1.0,) * 3,
            float2=(0.2, 0.6, 0),
            color_layer=(0.2, 0.6, 0.3),
            uv_map=(0.2, 0.6, 0),
            detail=(0.45,) * 3,
            primitive=(0.2, 0.6, 0.3),
            uv_attribute=(0.2, 0.6, 0),
        ).get(case)
        self.material.layoutChildren()
        self.parm = pass_fixture.parm(
            self.deletes[0], "dtldel" if case == "detail" else "primdel" if case == "primitive" else "ptdel"
        )

    def planes(self, frame):
        return {
            name: np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"]).copy()
            for name, p in frame["planes"].items()
        }

    def check(self, frame, status, broken=False, baseline=None):
        pixels = self.planes(frame)
        rgb = pixels["combined"]
        position = pixels["position"]
        assert status["state"] != "failed" and not status["error"], status
        assert status["geometry_resources"] == status["object_resources"] == 3, status
        assert all(np.isfinite(p).all() for p in pixels.values())
        assert bool(status["source_diagnostics"]) == broken, status["source_diagnostics"]
        for kind, x in enumerate((-1.6, 0, 1.6)):
            mask = (rgb[:, :, 3] > 0.999) & (np.abs(position[:, :, 0] - x) < 0.65)
            assert mask.sum() > 5, (kind, mask.sum())
            expected = (1, 0, 1) if broken and kind == 0 else self.expected
            if expected is not None:
                assert np.allclose(rgb[mask, :3], expected, atol=2e-5), (kind, rgb[mask, :3].mean(axis=0), expected)
            elif not (broken and kind == 0):
                # The authored field is a known linear function of local position.
                local = position[mask, :3].copy()
                local[:, 0] -= x
                expected = 0.5 + 0.2 * local
                # Spheres use constant point fields; compare their averaged value below.
                if kind == 0:
                    # Position is a single data sample, beauty integrates the
                    # pixel filter. Bound that sampling difference explicitly.
                    difference = np.abs(rgb[mask, :3] - expected)
                    assert difference.mean() < 0.0002 and difference.max() < 0.001, (
                        difference.mean(),
                        difference.max(),
                    )
        if not broken and baseline is not None:
            for name in ("combined", "position", "normal"):
                assert np.allclose(pixels[name], baseline[name], atol=2e-5), (name, "repair differs")
        if broken:
            assert all(d["resource"] for d in status["source_diagnostics"]), status["source_diagnostics"]
        return pixels
