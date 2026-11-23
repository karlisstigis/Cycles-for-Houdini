# SPDX-License-Identifier: Apache-2.0
"""Camera-derived heights with numeric, already-displaced reference triangles."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import math
import os
import time
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene

CASES = ("camera_depth", "camera_distance", "camera_vector", "camera_coordinate", "window")
POINTS = ((-0.65, -0.55, 0.0), (0.65, -0.55, 0.0), (0.65, 0.55, 0.0), (-0.65, 0.55, 0.0))


def geometry(positions):
    result = hou.Geometry()
    points = []
    for xyz in positions:
        point = result.createPoint()
        point.setPosition(tuple(float(x) for x in xyz))
        points.append(point)
    for corners in ((0, 1, 2), (0, 2, 3)):
        face = result.createPolygon()
        for index in corners:
            face.addVertex(points[index])
    result.addAttrib(hou.attribType.Point, "N", (0.0, 0.0, 1.0))
    return result


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


class Fixture:
    def __init__(self, device="CPU", width=128, height=96):
        self.width, self.height, self.device = width, height, device
        self.dest = artifacts_root() / "stage3/displacement-camera-coordinates"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.root = hou.node("/obj").createNode("subnet", "camera_coordinate_displacement")
        self.reference_root = hou.node("/obj").createNode("subnet", "numeric_camera_coordinate_reference")
        self.camera = self.root.createNode("cam", "camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=4.5, focal=50, aperture=41.4214))
        self.objects, self.reference_objects = [], []
        self.reference_stashes = []
        self.materials = []
        for index, tint in enumerate(((0.2, 0.5, 0.8), (0.2, 0.8, 0.35))):
            material = ch.create_material(name="camera_coordinate_" + str(index), shader="emission")
            pass_fixture.set_parameters(material.node("surface_shader"), dict(strength=1))
            material.node("surface_shader").parmTuple("color").set(tint)
            self.materials.append(material)
            obj = self.root.createNode(
                "geo", "target" if index == 0 else "independent_generated", run_init_scripts=False
            )
            pass_fixture.parm(obj, "tx").set(-0.9 if index == 0 else 0.9)
            stash = obj.createNode("stash", "SOURCE")
            pass_fixture.parm(stash, "stash").set(geometry(POINTS))
            stash.setDisplayFlag(True)
            stash.setRenderFlag(True)
            pass_fixture.parm(obj, "shop_materialpath").set(material.path())
            self.objects.append(obj)
            ref = self.reference_root.createNode("geo", "reference_" + str(index), run_init_scripts=False)
            ref_stash = ref.createNode("stash", "NUMERIC_REFERENCE")
            ref_stash.setDisplayFlag(True)
            ref_stash.setRenderFlag(True)
            ref_material = ch.create_material(name="camera_reference_" + str(index), shader="emission")
            pass_fixture.parm(ref_material.node("surface_shader"), "strength").set(1)
            ref_material.node("surface_shader").parmTuple("color").set(tint)
            pass_fixture.parm(ref, "shop_materialpath").set(ref_material.path())
            self.reference_objects.append(ref)
            self.reference_stashes.append(ref_stash)
            coordinate = material.createNode("cycles::texture_coordinate::1", "coordinates")
            split = material.createNode("cycles::separate_xyz::1", "component")
            combine = material.createNode("cycles::combine_xyz::1", "height_vector")
            displacement = material.createNode("cycles::vector_displacement::1", "offset")
            pass_fixture.set_parameters(displacement, dict(space="object", midlevel=0, scale=0.1))
            displacement.setNamedInput("vector", combine, "vector")
            material.node("surface_output").setNamedInput("displacement", displacement, "displacement")
            pass_fixture.parm(material, "cycles_displacement_method").set("true")
            if index == 0:
                self.coordinate, self.split, self.combine = coordinate, split, combine
                self.camera_info = material.createNode("cycles::camera_info::1", "camera_data")
            else:
                split.setNamedInput("vector", coordinate, "generated")
                combine.setNamedInput("z", split, "x")
                material.createNode("cycles::camera_info::1", "disconnected_camera_data")
        self.rop = self.make_rop(self.root)
        self.reference_rop = self.make_rop(self.reference_root)
        self.case = "camera_depth"
        self.configure(self.case)

    def make_rop(self, root):
        rop = menu_actions.create_rop()
        pass_fixture.set_parameters(
            rop,
            dict(
                objectroot=root.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=16,
                ipr_samples=16,
                render_threads=2,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                allowmotionblur=0,
                transparent_background=1,
                film_exposure=1,
                pass_position=1,
                pass_depth=1,
                use_texture_cache=0,
                world_source="color",
            ),
        )
        rop.parmTuple("background").set((0, 0, 0))
        device_selection.set_devices(rop, self.device if isinstance(self.device, list) else [self.device])
        return rop

    def configure(self, case):
        self.case = case
        pass_fixture.set_parameters(self.camera, dict(tx=0, ty=0, tz=5, rz=0, projection=1, orthowidth=4.5, focal=50))
        for rop in (self.rop, self.reference_rop):
            pass_fixture.parm(rop, "pixel_aspect").set(1)
        if case in ("camera_coordinate", "window"):
            self.split.setNamedInput("vector", self.coordinate, "camera" if case == "camera_coordinate" else "window")
            self.combine.setNamedInput("z", self.split, "z" if case == "camera_coordinate" else "y")
        elif case == "camera_vector":
            self.split.setNamedInput("vector", self.camera_info, "view_vector")
            self.combine.setNamedInput("z", self.split, "z")
        else:
            self.combine.setNamedInput(
                "z", self.camera_info, "view_z_depth" if case == "camera_depth" else "view_distance"
            )

    def target_height(self, point):
        camera = hou.Vector3(tuple(point)) * self.camera.worldTransform().inverted()
        x, y, z = float(camera[0]), float(camera[1]), -float(camera[2])
        if self.case == "camera_distance":
            value = math.sqrt(x * x + y * y + z * z)
        elif self.case == "camera_vector":
            value = z / math.sqrt(x * x + y * y + z * z)
        elif self.case == "window":
            aspect = self.width / self.height * pass_fixture.evaluate(self.rop, "pixel_aspect")
            if pass_fixture.evaluate(self.camera, "projection") == 1:
                value = 0.5 + y * aspect / pass_fixture.evaluate(self.camera, "orthowidth")
            else:
                value = 0.5 + y * pass_fixture.evaluate(self.camera, "focal") * aspect / (
                    z * pass_fixture.evaluate(self.camera, "aperture")
                )
        else:
            value = z
        return 0.1 * value

    def reference_update(self):
        for index, (obj, stash) in enumerate(zip(self.objects, self.reference_stashes)):
            positions = []
            for local in POINTS:
                world = hou.Vector3(local) * obj.worldTransform()
                z = self.target_height(world) if index == 0 else 0.1 * ((local[0] + 0.65) / 1.3)
                positions.append((world[0], world[1], world[2] + z))
            pass_fixture.parm(stash, "stash").set(geometry(positions))
        pass_fixture.parm(self.reference_rop, "pixel_aspect").set(pass_fixture.evaluate(self.rop, "pixel_aspect"))
        return native_scene.capture(self.reference_rop)

    def update(self, camera_only=False):
        return native_scene.capture(self.rop, camera_only=camera_only)

    def render(self, session, update):
        frame = session.wait(session.apply(update), timeout=120)
        deadline = time.monotonic() + 15
        while True:
            status = session.status()
            if status["state"] == "failed" or (
                status["state"] == "complete" and status["generation"] == frame["generation"]
            ):
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.005)
        assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
        return frame, status

    def compare(self, frame, reference):
        a, b = plane(frame, "combined"), plane(reference, "combined")
        mask = (a[..., 3] > 0.999) & (b[..., 3] > 0.999)
        assert mask.sum() > 500
        assert (a[mask, 2] > 0.75).sum() > 100 and (a[mask, 1] > 0.75).sum() > 100
        difference = float(np.max(np.abs(plane(frame, "position")[mask] - plane(reference, "position")[mask])))
        assert difference < 5e-5, difference
        np.testing.assert_allclose(plane(frame, "depth")[mask], plane(reference, "depth")[mask], atol=5e-5, rtol=0)
        rgba_mean = float(np.mean(np.abs(a - b)))
        assert rgba_mean < 0.0003, rgba_mean
        return dict(maximum_position_error=difference, mean_rgba_error=rgba_mean)

    def check(self, frame, target_enabled=True):
        """Check world-space heights, interpolated from independently moved vertices."""
        rgba, position = plane(frame, "combined"), plane(frame, "position")
        errors = []
        for index, obj in enumerate(self.objects):
            x = position[..., 0] - pass_fixture.evaluate(obj, "tx")
            y = position[..., 1]
            mask = (rgba[..., 3] > 0.999) & (np.abs(x) < 0.55) & (np.abs(y) < 0.45)
            assert mask.sum() > 100, (index, int(mask.sum()))
            u, v = (x[mask] + 0.65) / 1.3, (y[mask] + 0.55) / 1.1
            if index == 0:
                heights = [
                    self.target_height(hou.Vector3(p) * obj.worldTransform()) if target_enabled else 0 for p in POINTS
                ]
                expected = np.where(
                    u >= v,
                    heights[0] * (1 - u) + heights[1] * (u - v) + heights[2] * v,
                    heights[0] * (1 - v) + heights[2] * u + heights[3] * (v - u),
                )
                tint = (0.2, 0.5, 0.8)
            else:
                expected = 0.1 * u
                tint = (0.2, 0.8, 0.35)
            error = float(np.max(np.abs(position[mask, 2] - expected)))
            assert error < 5e-5, (self.case, index, error)
            np.testing.assert_allclose(rgba[mask, :3], np.broadcast_to(tint, (mask.sum(), 3)), atol=3e-5, rtol=0)
            errors.append(error)
        return dict(target_position_error=errors[0], independent_position_error=errors[1])
