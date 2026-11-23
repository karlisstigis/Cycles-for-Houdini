# SPDX-License-Identifier: Apache-2.0
"""Scene-query recovery with numeric pre-displaced geometry and surface controls."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import os
import time
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties
from camera_coordinate_displacement_fixture import Fixture as CameraFixture, geometry, plane, POINTS

QUERIES = ("raycast", "ambient_occlusion", "bevel")
QUERY_LABELS = dict(raycast="Raycast", ambient_occlusion="Ambient Occlusion", bevel="Bevel")


def box_geometry(size=(1.2, 1.0, 0.4)):
    g = hou.Geometry()
    normal = g.addAttrib(hou.attribType.Point, "N", (0.0, 0.0, 1.0))
    faces = [
        ((0, 0, 1), ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))),
        ((0, 0, -1), ((-1, 1, -1), (1, 1, -1), (1, -1, -1), (-1, -1, -1))),
        ((1, 0, 0), ((1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1))),
        ((-1, 0, 0), ((-1, -1, 1), (-1, 1, 1), (-1, 1, -1), (-1, -1, -1))),
        ((0, 1, 0), ((-1, 1, 1), (1, 1, 1), (1, 1, -1), (-1, 1, -1))),
        ((0, -1, 0), ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1))),
    ]
    for n, positions in faces:
        points = []
        for p in positions:
            point = g.createPoint()
            point.setPosition(tuple(p[i] * size[i] / 2 for i in range(3)))
            point.setAttribValue(normal, tuple(float(component) for component in n))
            points.append(point)
        for corners in ((0, 1, 2), (0, 2, 3)):
            face = g.createPolygon()
            for i in corners:
                face.addVertex(points[i])
    return g


class Fixture(CameraFixture):
    def __init__(self, device="CPU"):
        # Construct the established camera fixture without virtual configure
        # dispatch while this fixture's additional nodes are still incomplete.
        baseline = CameraFixture(device)
        self.__dict__.update(baseline.__dict__)
        self.dest = artifacts_root() / "stage3/displacement-scene-query"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.plane = geometry(POINTS)
        self.box = box_geometry()
        self.target_stash = self.objects[0].node("SOURCE")
        self.references = [hou.node(pass_fixture.evaluate(obj, "shop_materialpath")) for obj in self.reference_objects]
        self.builders = []
        for material in (self.materials[0], self.references[0]):
            nodes = dict(
                material=material, surface=material.node("surface_shader"), output=material.node("surface_output")
            )
            pass_fixture.parm(material, "cycles_pass_index").set(13)
            for kind in QUERIES:
                nodes[kind] = material.createNode("cycles::" + kind + "::1", "query_" + kind)
            # Direction is a LINK_NORMAL socket, deliberately without a
            # numeric pane parameter. Author the explicit world +Z direction
            # through a constant vector instead of replacing its default UI.
            nodes["ray_direction"] = material.createNode("cycles::combine_xyz::1", "ray_direction")
            pass_fixture.parm(nodes["ray_direction"], "z").set(1)
            nodes["raycast"].setNamedInput("direction", nodes["ray_direction"], "vector")
            nodes["bevel_z"] = material.createNode("cycles::separate_xyz::1", "bevel_z")
            nodes["bevel_z"].setNamedInput("vector", nodes["bevel"], "bevel")
            nodes["scaled"] = material.createNode("cycles::math::1", "scaled_query")
            pass_fixture.set_parameters(nodes["scaled"], dict(math_type="multiply", value2=0.1))
            nodes["add"] = material.createNode("cycles::math::1", "independent_height")
            pass_fixture.set_parameters(nodes["add"], dict(math_type="add", value1=0.25))
            nodes["multiply"] = material.createNode("cycles::math::1", "coupled_height")
            pass_fixture.set_parameters(nodes["multiply"], dict(math_type="multiply", value1=0.25))
            nodes["vector"] = material.createNode("cycles::combine_xyz::1", "height_vector")
            nodes["vector_add"] = material.createNode("cycles::vector_math::1", "independent_vector")
            pass_fixture.set_parameters(nodes["vector_add"], dict(math_type="add"))
            nodes["vector_add"].parmTuple("vector1").set((0, 0, 0.25))
            nodes["offset"] = material.createNode("cycles::vector_displacement::1", "query_offset")
            pass_fixture.set_parameters(nodes["offset"], dict(space="object", midlevel=0, scale=1))
            nodes["normal_offset"] = material.createNode("cycles::displacement::1", "normal_offset")
            pass_fixture.set_parameters(nodes["normal_offset"], dict(space="object", midlevel=0, scale=1, height=0.25))
            nodes["zero"] = material.createNode("cycles::value::1", "disabled_query")
            pass_fixture.parm(nodes["zero"], "value").set(0)
            self.builders.append(nodes)
        control = self.materials[1].node("offset")
        control.setInput(control.inputIndex("vector"), None)
        control.parmTuple("vector").set((0, 0, 0.12))
        pass_fixture.parm(control, "scale").set(1)
        for material in (self.materials[1], self.references[1]):
            pass_fixture.parm(material, "cycles_pass_index").set(29)
        for root in (self.root, self.reference_root):
            obj = root.createNode("geo", "query_occluder", run_init_scripts=False)
            pass_fixture.set_parameters(obj, dict(tx=-1.2, tz=1.2))
            stash = obj.createNode("stash", "OCCLUDER")
            pass_fixture.parm(stash, "stash").set(box_geometry((0.6, 1.4, 0.1)))
            stash.setDisplayFlag(True)
            stash.setRenderFlag(True)
            object_properties.install(obj)
            pass_fixture.set_parameters(obj, dict(cycles_visibility_override=1, cycles_visibility_camera=0))
        for rop in (self.rop, self.reference_rop):
            pass_fixture.set_parameters(
                rop,
                dict(
                    samples=32,
                    ipr_samples=32,
                    pass_normal=1,
                    pass_material_id=1,
                    resolution_percent=100,
                    ipr_resolution_percent=0,
                ),
            )
        self.configure("raycast", "valid")

    def configure(self, kind, mode="add", method="true", surface_query=True):
        if mode == "unused":
            surface_query = False
        self.kind, self.mode, self.method, self.surface_query = kind, mode, method, surface_query
        self.case = kind + ":" + mode + ":" + method
        self.shape = self.box if kind == "bevel" and mode != "normal" else self.plane
        pass_fixture.parm(self.target_stash, "stash").set(self.shape)
        for reference, nodes in enumerate(self.builders):
            material, output = nodes["material"], nodes["output"]
            pass_fixture.parm(material, "cycles_displacement_method").set("bump" if mode == "bump" else method)
            query = nodes[kind]
            pass_fixture.set_parameters(nodes["raycast"], dict(length=3))
            pass_fixture.set_parameters(nodes["ambient_occlusion"], dict(distance=3, samples=16))
            pass_fixture.set_parameters(nodes["bevel"], dict(radius=0.18, samples=8))
            pass_fixture.parm(nodes["zero"], "value").set(-1 if kind == "ambient_occlusion" else 0)
            for query_type, parm in (("raycast", "length"), ("ambient_occlusion", "distance"), ("bevel", "radius")):
                node = nodes[query_type]
                node.setInput(node.inputIndex(parm), None)
            if mode == "disabled":
                pass_fixture.parm(
                    query, "samples" if kind == "ambient_occlusion" else "length" if kind == "raycast" else "radius"
                ).set(0)
            elif mode == "linked_disabled":
                query.setNamedInput(
                    "length" if kind == "raycast" else "distance" if kind == "ambient_occlusion" else "radius",
                    nodes["zero"],
                    "value",
                )
            elif mode == "global_ao":
                pass_fixture.parm(nodes["ambient_occlusion"], "distance").set(0)
            elif mode == "linked_global_ao":
                pass_fixture.parm(nodes["zero"], "value").set(0)
                nodes["ambient_occlusion"].setNamedInput("distance", nodes["zero"], "value")
            socket = "is_hit" if kind == "raycast" else "ao" if kind == "ambient_occlusion" else "bevel"
            if surface_query:
                nodes["surface"].setNamedInput("color", query, socket)
            else:
                nodes["surface"].setInput(nodes["surface"].inputIndex("color"), None)
                nodes["surface"].parmTuple("color").set((0.2, 0.5, 0.8))
            source, scalar_socket = (nodes["bevel_z"], "z") if kind == "bevel" else (query, socket)
            nodes["scaled"].setNamedInput("value1", source, scalar_socket)
            for name in ("add", "multiply"):
                nodes[name].setNamedInput("value2", nodes["scaled"], "value")
            nodes["vector"].setInput(nodes["vector"].inputIndex("z"), None)
            nodes["offset"].setInput(nodes["offset"].inputIndex("vector"), None)
            nodes["offset"].parmTuple("vector").set((0, 0, 0.25))
            if mode not in ("valid", "disconnected", "unused"):
                nodes["vector"].setNamedInput(
                    "z",
                    nodes["add"]
                    if mode in ("add", "global_ao", "linked_global_ao")
                    else nodes["multiply"]
                    if mode == "coupled"
                    else nodes["scaled"],
                    "value",
                )
                nodes["offset"].setNamedInput("vector", nodes["vector"], "vector")
            if mode == "vector_add":
                nodes["vector_add"].setNamedInput("vector2", nodes["vector"], "vector")
                nodes["offset"].setNamedInput("vector", nodes["vector_add"], "vector")
            output.setInput(output.inputIndex("displacement"), None)
            if not reference or mode == "bump":
                if mode == "normal":
                    nodes["normal_offset"].setNamedInput("normal", query, socket)
                    output.setNamedInput("displacement", nodes["normal_offset"], "displacement")
                else:
                    output.setNamedInput("displacement", nodes["offset"], "displacement")

    @property
    def warning(self):
        return self.mode in ("whole", "add", "vector_add", "coupled", "normal", "global_ao", "linked_global_ao")

    def vertex_height(self, point):
        if self.mode in (
            "valid",
            "disconnected",
            "unused",
            "add",
            "vector_add",
            "normal",
            "global_ao",
            "linked_global_ao",
        ):
            return 0.25
        if self.mode in ("disabled", "linked_disabled"):
            return 0.1 * point.attribValue("N")[2] if self.kind == "bevel" else 0 if self.kind == "raycast" else 0.1
        return 0

    def reference_update(self):
        for index, (obj, stash) in enumerate(zip(self.objects, self.reference_stashes)):
            source = self.shape if index == 0 else self.plane
            g = hou.Geometry()
            g.merge(source)
            for point in g.points():
                height = self.vertex_height(point) if index == 0 else 0.12
                world = point.position() * obj.worldTransform()
                point.setPosition((world[0], world[1], world[2] + height))
            pass_fixture.parm(stash, "stash").set(g)
        return native_scene.capture(self.reference_rop)

    def render(self, session, update):
        # A fresh OptiX device can spend several minutes compiling its first
        # scene-query pipeline before the renderer starts producing samples.
        frame = session.wait(session.apply(update), timeout=300)
        deadline = time.monotonic() + 15
        while True:
            status = session.status()
            if status["state"] == "failed" or (
                status["state"] == "complete" and status["generation"] == frame["generation"]
            ):
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.005)
        assert status["state"] == "complete" and not status["error"], status
        return frame, status

    def check_diagnostics(self, issues, warning=None):
        assert bool(issues) == (self.warning if warning is None else warning), (self.case, issues)
        if issues:
            assert all(
                d["code"] == "material.displacement_scene_query"
                and d["severity"] == "warning"
                and "BVH" in d["message"]
                and QUERY_LABELS[self.kind] in d["message"]
                and d["source"] == self.materials[0].path()
                and d["action"]
                for d in issues
            ), issues

    def check(self, frame, status):
        issues = status["source_diagnostics"]
        self.check_diagnostics(issues)
        assert not status["error"] and status["state"] != "failed", status
        rgba, position = plane(frame, "combined"), plane(frame, "position")
        assert np.isfinite(rgba).all() and np.isfinite(position).all()
        target = (rgba[..., 3] > 0.999) & (np.abs(position[..., 0] + 0.9) < 0.45) & (np.abs(position[..., 1]) < 0.35)
        control = (rgba[..., 3] > 0.999) & (np.abs(position[..., 0] - 0.9) < 0.45) & (np.abs(position[..., 1]) < 0.35)
        assert target.sum() > 100 and control.sum() > 100
        z = (0.2 if self.kind == "bevel" and self.mode != "normal" else 0) + self.vertex_height(self.shape.points()[0])
        np.testing.assert_allclose(position[target, 2], z, atol=3e-5, rtol=0, err_msg=self.case)
        np.testing.assert_allclose(position[control, 2], 0.12, atol=3e-5, rtol=0)
        np.testing.assert_allclose(plane(frame, "material_id")[target, 0], 13, atol=1e-5, rtol=0)
        np.testing.assert_allclose(plane(frame, "material_id")[control, 0], 29, atol=1e-5, rtol=0)
        return dict(
            target_height=float(np.median(position[target, 2])),
            independent_height=float(np.median(position[control, 2])),
            diagnostics=issues,
        )

    def compare(self, actual, reference):
        errors = {}
        for name in ("combined", "position", "normal", "material_id"):
            a, b = plane(actual, name), plane(reference, name)
            error = float(np.max(np.abs(a - b)))
            assert error < 6e-5, (self.case, name, error)
            errors[name] = error
        return errors
