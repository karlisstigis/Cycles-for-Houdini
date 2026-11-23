# SPDX-License-Identifier: Apache-2.0
"""Native disconnected polygons, seams and mixed components for island shading."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, object_properties


class Fixture:
    width, height = 210, 120
    centers = (-2.0, 0.0, 2.0)

    def __init__(self, device):
        self.dest = artifacts_root() / "examples/mesh-islands"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.material = ch.create_material(name="mesh_islands", shader="emission")
        self.geometry = self.material.createNode("cycles::geometry::1", "island_input")
        self.unsupported = self.material.createNode("cycles::texture_coordinate::1", "unsupported_instancer_coordinate")
        pass_fixture.parm(self.unsupported, "from_dupli").set(1)
        self.emission = self.material.node("surface_shader")
        pass_fixture.parm(self.emission, "strength").set(1)
        self.emission.setNamedInput("color", self.geometry, "random_per_island")
        self.displacement = self.material.createNode("cycles::displacement::1", "island_displacement")
        pass_fixture.set_parameters(self.displacement, dict(height=0.15, midlevel=0, scale=1, space="object"))
        self.other = ch.create_material(name="other_island_assignment", shader="emission")
        pass_fixture.parm(self.other.node("surface_shader"), "strength").set(1)
        self.other.node("surface_shader").setNamedInput(
            "color", self.other.createNode("cycles::geometry::1"), "random_per_island"
        )
        self.obj = hou.node("/obj").createNode("geo", "disconnected_pieces", run_init_scripts=False)
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.material.path())
        self.stash = self.obj.createNode("stash", "authored_polygons")
        self.output = self.obj.createNode("switch", "render_components")
        self.output.setInput(0, self.stash)
        self.points = self.obj.createNode("add", "loose_points")
        pass_fixture.parm(self.points, "points").set(1)
        self.points.parmTuple("pt0").set((-1.0, -1.25, 0))
        self.curve = self.obj.createNode("line", "hair")
        self.curve.parmTuple("origin").set((0.3, -1.25, 0))
        self.curve.parmTuple("dir").set((1, 0, 0))
        pass_fixture.parm(self.curve, "dist").set(1)
        mixed = self.obj.createNode("merge", "mixed_components")
        mixed.setInput(0, self.stash)
        mixed.setInput(1, self.points)
        mixed.setInput(2, self.curve)
        self.output.setInput(1, self.points)
        self.output.setInput(2, self.curve)
        self.output.setInput(3, mixed)
        targets = self.obj.createNode("add", "two_packed_targets")
        pass_fixture.parm(targets, "points").set(2)
        targets.parmTuple("pt0").set((0, -0.9, 0))
        targets.parmTuple("pt1").set((0, 0.9, 0))
        self.copies = self.obj.createNode("copytopoints", "packed_pieces")
        self.copies.setInput(0, self.stash)
        self.copies.setInput(1, targets)
        pass_fixture.parm(self.copies, "pack").set(1)
        self.output.setInput(4, self.copies)
        self.output.setRenderFlag(True)
        self.output.setDisplayFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "island_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=6, projection=1, orthowidth=7))
        self.rop = menu_actions.create_rop()
        self.rop.setName("mesh_island_test", unique_name=True)
        device_selection.set_devices(self.rop, [device])
        set_rop_devices(self.rop, device)
        configure_render(
            self.rop,
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=1,
                ipr_samples=1,
                allowmotionblur=0,
                use_denoise=0,
                ipr_use_denoise=0,
                pass_position=1,
                point_radius=0.12,
                curve_width=0.09,
                film_exposure=1,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_texture_cache=0,
                render_threads=2,
                transparent_background=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.set_mesh()

    def set_mesh(self, *, joined=False, reverse=False, deform=0.0, assignments=False, seams=False, fault=None):
        geo = hou.Geometry()
        points = []
        for center in self.centers:
            for y in (-0.7, 0.0, 0.7):
                for x in (center - 0.7, center + 0.7):
                    p = geo.createPoint()
                    p.setPosition((x, y, deform * (x * x + 0.3 * y)))
                    points.append(p)
        faces = []
        for start in (0, 6, 12):
            for row in (0, 2):
                faces.append((start + row, start + row + 1, start + row + 3, start + row + 2))
        if joined:
            faces.append((5, 3, 6, 8))  # Join first two pieces with consistently oriented shared edges.
        if fault == "winding":
            faces[0] = tuple(reversed(faces[0]))
        elif fault == "nonmanifold":
            faces.append(tuple(reversed(faces[0])))
        elif fault == "repeated_point":
            faces[0] = (0, 1, 1, 3, 2)
        if reverse:
            faces.reverse()
        if seams:
            uv = geo.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
        if assignments:
            binding = geo.addAttrib(hou.attribType.Prim, "shop_materialpath", "")
        for index, face in enumerate(faces):
            poly = geo.createPolygon()
            # Houdini polygons use clockwise vertex winding for a +Z normal.
            for point in reversed(face):
                vertex = poly.addVertex(points[point])
                if seams:
                    vertex.setAttribValue(uv, (float(index % 2), 0.2, 0.0))
            if assignments:
                poly.setAttribValue(binding, (self.other if index % 2 else self.material).path())
        pass_fixture.parm(self.stash, "stash").set(geo)

    def shade(self, mode="islands"):
        self.emission.setNamedInput(
            "color",
            self.unsupported if mode == "unsupported" else self.geometry,
            "generated" if mode == "unsupported" else "random_per_island" if mode == "islands" else "pointiness",
        )
        if mode == "constant":
            self.emission.setInput(0, None)
            self.emission.parmTuple("color").set((0.3, 0.3, 0.3))

    def displace(self, enabled=True, from_island=False):
        output = self.material.node("surface_output")
        if enabled:
            if from_island:
                self.displacement.setNamedInput("height", self.geometry, "random_per_island")
            else:
                self.displacement.setInput(0, None)
                pass_fixture.parm(self.displacement, "height").set(0.15)
            output.setNamedInput("displacement", self.displacement, "displacement")
            pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        else:
            output.setInput(output.inputIndex("displacement"), None)
            pass_fixture.parm(self.material, "cycles_displacement_method").set("bump")

    def inspect(self, frame, status, *, warning=False, joined=False, packed=False, expected=None, displaced=False):
        assert status["state"] != "failed" and bool(status["source_diagnostics"]) == warning, status

        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])

        color, position = plane("combined"), plane("position")
        assert np.isfinite(color).all() and np.isfinite(position).all()
        values = []
        for y in (-0.9, 0.9) if packed else (0.0,):
            row = []
            for x in self.centers:
                mask = (
                    (color[:, :, 3] > 0.99)
                    & (np.abs(position[:, :, 0] - x) < 0.38)
                    & (np.abs(position[:, :, 1] - y) < 0.28)
                )
                assert mask.sum() > 30, (x, y, int(mask.sum()))
                selected = color[mask, :3]
                assert np.ptp(selected) < 5e-5, selected[[0, -1]]
                value = float(selected.mean())
                assert 0 <= value <= 1, value
                row.append(value)
                if displaced:
                    expected_height = value if displaced == "island" else 0.15
                    np.testing.assert_allclose(position[mask, 2], expected_height, atol=3e-5, rtol=0)
            values.append(row)
        result = np.array(values)
        if joined:
            assert abs(result[0, 0] - result[0, 1]) < 5e-5, result
        else:
            assert min(abs(a - b) for i, a in enumerate(result[0]) for b in result[0, i + 1 :]) > 0.01, result
        if packed:
            np.testing.assert_allclose(result[0], result[1], atol=5e-5, rtol=0)
        if expected is not None:
            np.testing.assert_allclose(result, np.broadcast_to(expected, result.shape), atol=5e-5, rtol=0)
        return result.tolist()

    def add_heavy(self):
        obj = hou.node("/obj").createNode("geo", "retained_heavy_islands", run_init_scripts=False)
        object_properties.install(obj)
        pass_fixture.set_parameters(obj, dict(cycles_subdivision_override=0))
        material = ch.create_material(name="heavy_islands", shader="emission")
        pass_fixture.parm(material.node("surface_shader"), "strength").set(1)
        material.node("surface_shader").setNamedInput(
            "color", material.createNode("cycles::geometry::1"), "random_per_island"
        )
        pass_fixture.parm(obj, "shop_materialpath").set(material.path())
        self.heavy = obj.createNode("torus", "million_triangle_prototype")
        pass_fixture.set_parameters(self.heavy, dict(type=0, rows=512, cols=1024))
        targets = obj.createNode("grid", "twelve_targets")
        pass_fixture.set_parameters(targets, dict(type=0, rows=3, cols=4))
        targets.parmTuple("t").set((0, 0, 20))
        copies = obj.createNode("copytopoints")
        copies.setInput(0, self.heavy)
        copies.setInput(1, targets)
        pass_fixture.parm(copies, "pack").set(1)
        copies.setRenderFlag(True)
        copies.setDisplayFlag(True)
        pass_fixture.parm(self.rop, "vobject").set(self.obj.path() + " " + obj.path())
        return obj
