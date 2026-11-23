# SPDX-License-Identifier: Apache-2.0
"""Analytic and independent NumPy references for original-polygon Pointiness."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import itertools, os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, native_scene, _native


def reference_pointiness(positions, faces):
    p = np.asarray(positions, float)
    unique, inverse = np.unique(p, axis=0, return_inverse=True)
    normal = np.zeros_like(p)
    adjacency = np.zeros((len(unique), len(unique)))
    for face in faces:
        v = p[np.asarray(face)]
        n = np.cross(v, np.roll(v, -1, axis=0)).sum(axis=0)
        length = np.linalg.norm(n)
        n = n / length if length else n
        previous = np.roll(v, 1, axis=0) - v
        following = np.roll(v, -1, axis=0) - v
        for i, index in enumerate(face):
            a, b = previous[i], following[i]
            la, lb = np.linalg.norm(a), np.linalg.norm(b)
            angle = np.arccos(np.clip(np.dot(a, b) / (la * lb), -1, 1)) if la and lb else np.pi / 2
            normal[index] += angle * n
            x, y = inverse[index], inverse[face[(i + 1) % len(face)]]
            adjacency[x, y] = adjacency[y, x] = 1
    length = np.linalg.norm(normal, axis=1)
    normal = np.divide(normal, length[:, None], out=np.zeros_like(normal), where=length[:, None] > 0)
    merged = np.zeros_like(unique)
    np.add.at(merged, inverse, normal)
    length = np.linalg.norm(merged, axis=1)
    merged = np.divide(merged, length[:, None], out=np.zeros_like(merged), where=length[:, None] > 0)
    delta = unique[None, :, :] - unique[:, None, :]
    length = np.linalg.norm(delta, axis=2)
    direction = np.divide(delta, length[:, :, None], out=np.zeros_like(delta), where=length[:, :, None] > 0)
    degree = adjacency.sum(axis=1)
    average = np.divide(
        (direction * adjacency[:, :, None]).sum(axis=1),
        degree[:, None],
        out=np.zeros_like(merged),
        where=degree[:, None] > 0,
    )
    raw = np.arccos(np.clip((merged * average).sum(axis=1), -1, 1)) / np.pi
    raw[degree == 0] = 0
    blurred = (raw + adjacency @ raw) / (degree + 1)
    return blurred[inverse].astype(np.float32)


class Fixture:
    width, height = 128, 128

    def __init__(self, device="CPU"):
        self.device = device
        self.dest = artifacts_root() / "stage3/materials/pointiness"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.scene = hou.node("/obj").createNode("subnet", "pointiness_fixture")
        self.obj = self.scene.createNode("geo", "surface", run_init_scripts=False)
        self.stash = self.obj.createNode("stash", "POLYGONS")
        self.wave = self.obj.createNode("attribwrangle", "ANIMATED_SHAPE")
        self.wave.setInput(0, self.stash)
        pass_fixture.parm(self.wave, "snippet").set("")
        targets = self.obj.createNode("grid", "targets")
        pass_fixture.set_parameters(targets, dict(type=0, orient=0, rows=2, cols=2))
        targets.parmTuple("size").set((2.4, 2.4))
        orient = self.obj.createNode("attribwrangle", "orient")
        orient.setInput(0, targets)
        pass_fixture.parm(orient, "snippet").set("v@N={0,0,1};v@up={0,1,0};f@pscale=.45;")
        self.copies = self.obj.createNode("copytopoints", "packed")
        self.copies.setInput(0, self.wave)
        self.copies.setInput(1, orient)
        pass_fixture.parm(self.copies, "pack").set(1)
        self.line = self.obj.createNode("line", "hair")
        self.line.parmTuple("origin").set((-1.65, -0.5, 0))
        self.line.parmTuple("dir").set((0, 1, 0))
        pass_fixture.parm(self.line, "dist").set(1)
        self.point = self.obj.createNode("add", "point")
        pass_fixture.parm(self.point, "points").set(1)
        self.point.parmTuple("pt0").set((1.65, 0, 0))
        self.mixed = self.obj.createNode("merge", "mixed")
        for i, node in enumerate((self.wave, self.line, self.point)):
            self.mixed.setInput(i, node)
        self.output = self.obj.createNode("switch", "OUT")
        for i, node in enumerate((self.wave, self.copies, self.mixed)):
            self.output.setInput(i, node)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.mat = ch.create_material(name="mesh_pointiness", shader="emission")
        self.emission = self.mat.node("surface_shader")
        pass_fixture.parm(self.emission, "strength").set(1)
        self.geometry = self.mat.createNode("cycles::geometry::1", "geometry")
        self.reference = self.mat.createNode("cycles::attribute::1", "independent_reference")
        pass_fixture.parm(self.reference, "attribute").set("reference_curvature")
        self.user = self.mat.createNode("cycles::attribute::1", "ordinary_pointiness_attribute")
        pass_fixture.parm(self.user, "attribute").set("pointiness")
        self.combine = self.mat.createNode("cycles::combine_xyz::1", "independent_names")
        self.combine.setNamedInput("x", self.geometry, "pointiness")
        self.combine.setNamedInput("y", self.user, "fac")
        pass_fixture.parm(self.combine, "z").set(0.2)
        self.displacement = self.mat.createNode("cycles::displacement::1", "intrinsic_displacement")
        pass_fixture.set_parameters(self.displacement, dict(scale=0.2, midlevel=0.5, space="object"))
        self.displacement.setNamedInput("height", self.geometry, "pointiness")
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.mat.path())
        self.camera = self.scene.createNode("cam", "camera")
        pass_fixture.set_parameters(self.camera, dict(tz=6, projection=1, orthowidth=4.4))
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
                samples=4,
                ipr_samples=4,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                allowmotionblur=0,
                pass_position=1,
                pass_normal=1,
                use_texture_cache=0,
                render_threads=2,
                point_radius=0.15,
                curve_width=0.12,
                transparent_background=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.rop.parmTuple("lightstrength").set((0, 0, 0))
        self.shade()
        self.set_shape()

    def shade(self, mode="pointiness"):
        if mode == "constant":
            self.emission.setInput(0, None)
            self.emission.parmTuple("color").set((0.3, 0.3, 0.3))
        else:
            node, socket = {
                "pointiness": (self.geometry, "pointiness"),
                "reference": (self.reference, "fac"),
                "names": (self.combine, "vector"),
                "user": (self.user, "fac"),
            }[mode]
            self.emission.setNamedInput("color", node, socket)
        self.shading = mode

    def displace(self, enabled=True):
        output = self.mat.node("surface_output")
        if enabled:
            output.setNamedInput("displacement", self.displacement, "displacement")
        else:
            output.setInput(output.inputNames().index("displacement"), None)
        pass_fixture.parm(self.mat, "cycles_displacement_method").set("both" if enabled else "bump")

    def set_shape(
        self,
        kind="octa",
        *,
        reverse=False,
        deform=0.0,
        duplicate=False,
        normals=None,
        user_value=None,
        seams=False,
        reference_shift=0.0,
    ):
        if kind == "octa":
            positions = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
            faces = []
            for indices in itertools.product((0, 1), (2, 3), (4, 5)):
                a, b, c = np.asarray(positions)[list(indices)]
                face = list(indices)
                if np.dot(np.cross(b - a, c - a), a + b + c) < 0:
                    face.reverse()
                faces.append(face)
        else:
            positions = [
                (x, y, deform * (x * x - 0.7 * y * y + 0.2 * x * y)) for y in (-1.0, 0.0, 1.0) for x in (-1.0, 0.0, 1.0)
            ]
            faces = [[a, a + 1, a + 4, a + 3] for a in (0, 1, 3, 4)]
        if reverse:
            faces = [list(reversed(face)) for face in faces]
        if duplicate:
            original = positions
            positions = []
            rebuilt = []
            for face in faces:
                start = len(positions)
                positions.extend(original[i] for i in face)
                rebuilt.append(list(range(start, start + len(face))))
            faces = rebuilt
        values = reference_pointiness(positions, faces) + reference_shift
        geo = hou.Geometry()
        geo.addAttrib(hou.attribType.Point, "reference_curvature", 0.0)
        if normals is not None:
            geo.addAttrib(hou.attribType.Point, "N", "invalid" if normals == "bad_type" else (0.0, 0.0, 1.0))
        if user_value is not None:
            geo.addAttrib(hou.attribType.Point, "pointiness", user_value)
        uv = geo.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0)) if seams else None
        points = []
        for i, p in enumerate(positions):
            point = geo.createPoint()
            point.setPosition(p)
            point.setAttribValue("reference_curvature", float(values[i]))
            points.append(point)
            if normals == "inverted":
                point.setAttribValue("N", (0.0, 0.0, -1.0))
            elif normals == "radial":
                point.setAttribValue("N", tuple(np.asarray(p) / np.linalg.norm(p)))
            elif normals == "bad_value":
                point.setAttribValue("N", (float("nan"), 0.0, 1.0) if i == 0 else (0.0, 0.0, 1.0))
        for number, face in enumerate(faces):
            poly = geo.createPolygon()
            for i in reversed(face):
                vertex = poly.addVertex(points[i])
                if uv:
                    vertex.setAttribValue(uv, (float(number % 2), 0.2, 0.0))
        self.positions, self.faces, self.expected = np.asarray(positions), faces, values
        pass_fixture.parm(self.stash, "stash").set(geo)

    def update(self):
        return native_scene.capture(self.rop)

    def planes(self, frame):
        return {
            name: np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"]).copy()
            for name, p in frame["planes"].items()
        }

    def gray(self, frame, value, *, magenta=False):
        image = self.planes(frame)["combined"]
        assert np.isfinite(image).all()
        visible = image[:, :, 3] > 0.999
        match = visible & np.all(np.isclose(image[:, :, :3], (value,) * 3, atol=3e-5, rtol=0), axis=2)
        assert match.sum() > 40, (value, np.unique(np.round(image[visible, :3], 4), axis=0))
        if not magenta:
            assert not np.any(visible & np.all(np.isclose(image[:, :, :3], (1, 0, 1), atol=1e-4), axis=2))

    def owned(self, session):
        return _native.snapshot_info(session._controller.acknowledged)

    def refresh_reference(self):
        positions = np.asarray([tuple(p.position()) for p in self.wave.geometry().points()])
        self.expected = reference_pointiness(positions, self.faces)
        geo = hou.Geometry()
        geo.merge(self.stash.geometry())
        for p, value in zip(geo.points(), self.expected):
            p.setAttribValue("reference_curvature", float(value))
        pass_fixture.parm(self.stash, "stash").set(geo)

    def add_heavy(self):
        obj = self.scene.createNode("geo", "heavy_retained", run_init_scripts=False)
        torus = obj.createNode("torus")
        pass_fixture.set_parameters(torus, dict(type=0, rows=512, cols=1024))
        targets = obj.createNode("grid")
        pass_fixture.set_parameters(targets, dict(type=0, rows=3, cols=4))
        targets.parmTuple("t").set((0, 0, 20))
        copies = obj.createNode("copytopoints")
        copies.setInput(0, torus)
        copies.setInput(1, targets)
        pass_fixture.parm(copies, "pack").set(1)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        mat = ch.create_material(name="retained_constant", shader="emission")
        mat.node("surface_shader").parmTuple("color").set((0.3, 0.4, 0.5))
        pass_fixture.parm(mat.node("surface_shader"), "strength").set(1)
        pass_fixture.parm(obj, "shop_materialpath").set(mat.path())
        self.heavy = torus
        return obj
