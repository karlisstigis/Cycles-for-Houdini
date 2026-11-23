# SPDX-License-Identifier: Apache-2.0
"""Native colored enclosure and independent geometric ray/image expectations."""

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import functools, math
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import camera_properties

COLORS = np.array(
    ((0.8, 0.1, 0.1), (0.1, 0.8, 0.1), (0.1, 0.1, 0.8), (0.8, 0.8, 0.1), (0.8, 0.1, 0.8), (0.1, 0.8, 0.8)), np.float64
)
MODELS = tuple(kind for kind, _ in camera_properties.PANORAMA_TYPES)


class Fixture:
    def __init__(self, device, width=128, height=64):
        hou.setFps(24)
        hou.setFrame(1)
        self.objects = []
        self.sops = []
        for axis in range(3):
            for sign in (1, -1):
                index = axis * 2 + (sign < 0)
                material = ch.create_material(name="panorama_face_" + str(index), shader="emission")
                material.node("surface_shader").parmTuple("color").set(tuple(COLORS[index]))
                pass_fixture.parm(material.node("surface_shader"), "strength").set(1)
                obj = hou.node("/obj").createNode("geo", "panorama_wall_" + str(index), run_init_scripts=False)
                pass_fixture.parm(obj, "shop_materialpath").set(material.path())
                t = [0, 0, 0]
                t[axis] = sign * 3
                obj.parmTuple("t").set(t)
                obj.parmTuple("r").set((0, 90, 0) if axis == 0 else (90, 0, 0) if axis == 1 else (0, 0, 0))
                grid = obj.createNode("grid", "native_wall")
                pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
                grid.parmTuple("size").set((6, 6))
                grid.setDisplayFlag(True)
                grid.setRenderFlag(True)
                self.objects.append(obj)
                self.sops.append(grid)
        self.camera = hou.node("/obj").createNode("cam", "panorama_camera")
        camera_properties.install(self.camera)
        pass_fixture.set_parameters(self.camera, dict(cycles_projection="panorama", cycles_dof=0, near=0.01, far=100))
        self.rop = hou.node("/out").createNode("cycles_ipr", "native_panorama")
        # The default this test was written for (before the ROP took Blender's defaults).
        self.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                source_mode="objects",
                vobject=" ".join(o.path() for o in self.objects),
                camera=self.camera.path(),
                resx=width,
                resy=height,
                samples=1024,
                ipr_samples=1024,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                use_material_assignments=1,
                pass_position=1,
                pass_depth=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.configure("equirectangular")

    def configure(self, model, **overrides):
        spec = dict(
            model=model,
            fov=180.0,
            lens=10.5,
            latitude_min=-90.0,
            latitude_max=90.0,
            longitude_min=-180.0,
            longitude_max=180.0,
            u_min=-180.0,
            u_max=180.0,
            v_min=-1.0,
            v_max=1.0,
            radius=1.0,
            fit="auto",
            sensor_width=36.0,
            sensor_height=24.0,
            coefficients=tuple(camera_properties.POLYNOMIAL_DEFAULTS),
            pixel_aspect=1.0,
            angle=0.0,
            origin=(0.0, 0.0, 0.0),
        )
        spec.update(overrides)
        self.spec = spec
        for name in (
            "tx",
            "ty",
            "tz",
            "ry",
            "cycles_fisheye_fov",
            "cycles_fisheye_lens",
            "cycles_longitude_min",
            "cycles_longitude_max",
            "cycles_latitude_min",
            "cycles_latitude_max",
        ):
            pass_fixture.parm(self.camera, name).deleteAllKeyframes()
        pass_fixture.set_parameters(
            self.camera,
            dict(
                cycles_projection="panorama",
                cycles_panorama_type=model,
                cycles_fisheye_fov=spec["fov"],
                cycles_fisheye_lens=spec["lens"],
                cycles_latitude_min=spec["latitude_min"],
                cycles_latitude_max=spec["latitude_max"],
                cycles_longitude_min=spec["longitude_min"],
                cycles_longitude_max=spec["longitude_max"],
                cycles_cylindrical_u_min=spec["u_min"],
                cycles_cylindrical_u_max=spec["u_max"],
                cycles_cylindrical_v_min=spec["v_min"],
                cycles_cylindrical_v_max=spec["v_max"],
                cycles_cylindrical_radius=spec["radius"],
                cycles_sensor_fit=spec["fit"],
                cycles_sensor_width=spec["sensor_width"],
                cycles_sensor_height=spec["sensor_height"],
                ry=spec["angle"],
            ),
        )
        self.camera.parmTuple("t").set(spec["origin"])
        for i, value in enumerate(spec["coefficients"]):
            pass_fixture.parm(self.camera, "cycles_fisheye_k" + str(i)).deleteAllKeyframes()
            pass_fixture.parm(self.camera, "cycles_fisheye_k" + str(i)).set(value)
        pass_fixture.parm(self.rop, "pixel_aspect").set(spec["pixel_aspect"])
        return spec


def directions(spec, u, v, width, height):
    """Analytic ray geometry; no integration/engine camera or inverse helper."""
    model = spec["model"]
    valid = np.ones_like(u, dtype=bool)
    if model == "equirectangular":
        longitude = (
            -math.radians(spec["longitude_min"]) - math.radians(spec["longitude_max"] - spec["longitude_min"]) * u
        )
        latitude = (
            math.pi / 2
            - math.radians(spec["latitude_min"])
            - math.radians(spec["latitude_max"] - spec["latitude_min"]) * v
        )
        d = np.stack(
            (-np.sin(latitude) * np.sin(longitude), np.cos(latitude), -np.sin(latitude) * np.cos(longitude)), axis=-1
        )
    elif model == "central_cylindrical":
        longitude = -math.radians(spec["u_min"]) - math.radians(spec["u_max"] - spec["u_min"]) * u
        slope = (spec["v_min"] + (spec["v_max"] - spec["v_min"]) * v) / spec["radius"]
        d = np.stack((-np.sin(longitude), slope, -np.cos(longitude)), axis=-1)
    elif model == "equiangular_cubemap_face":
        d = np.stack((np.tan((u - 0.5) * math.pi / 2), np.tan((v - 0.5) * math.pi / 2), -np.ones_like(u)), axis=-1)
    elif model == "mirrorball":
        x = 2 * u - 1
        z = 2 * v - 1
        r2 = x * x + z * z
        valid = r2 <= 1
        ny = -np.sqrt(np.maximum(1 - r2, 0))
        d = np.stack((-2 * ny * x, -2 * ny * z, 1 - 2 * ny * ny), axis=-1)
    else:
        if model == "fisheye_equidistant":
            x = 2 * u - 1
            y = 2 * v - 1
        else:
            aspect = width * spec["pixel_aspect"] / height
            fit = spec["fit"]
            horizontal = fit == "horizontal" or (fit == "auto" and aspect > 1)
            size = spec["sensor_height"] if fit == "vertical" else spec["sensor_width"]
            sw, sh = (size, size / aspect) if horizontal else (size * aspect, size)
            x = (u - 0.5) * sw
            y = (v - 0.5) * sh
        r = np.sqrt(x * x + y * y)
        fov = math.radians(spec["fov"])
        if model == "fisheye_equidistant":
            angle = r * fov / 2
            valid = r <= 1
        elif model == "fisheye_equisolid":
            angle = 2 * np.arcsin(np.clip(r / (2 * spec["lens"]), 0, 1))
            valid = r <= 2 * spec["lens"] * math.sin(fov / 4)
        elif model == "fisheye_lens_polynomial":
            angle = -sum(k * r**i for i, k in enumerate(spec["coefficients"]))
            valid = np.abs(angle) <= fov / 2
        else:
            raise ValueError(model)
        q = np.divide(np.sin(angle), r, out=np.zeros_like(r), where=r != 0)
        d = np.stack((x * q, y * q, -np.cos(angle)), axis=-1)
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    a = math.radians(spec["angle"])
    cos = math.cos(a)
    sin = math.sin(a)
    return np.stack((cos * d[..., 0] + sin * d[..., 2], d[..., 1], -sin * d[..., 0] + cos * d[..., 2]), axis=-1), valid


def ray_pixels(spec, u, v, width, height):
    d, valid = directions(spec, u, v, width, height)
    origin = np.array(spec["origin"])
    planes = np.where(d >= 0, 3.0, -3.0)
    distance = np.divide(planes - origin, d, out=np.full_like(d, np.inf), where=d != 0)
    axis = np.argmin(distance, axis=-1)
    t = np.take_along_axis(distance, axis[..., None], axis=-1)[..., 0]
    sign = np.take_along_axis(d, axis[..., None], axis=-1)[..., 0] < 0
    index = axis * 2 + sign
    rgb = COLORS[index]
    valid &= (t >= spec.get("near", 0.01)) & (t <= spec.get("far", 100))
    return np.where(valid[..., None], rgb, 0), origin + d * t[..., None], t, valid


def reference(spec, width=128, height=64, samples=16):
    y, x = np.indices((height, width), dtype=np.float64)
    image = np.zeros((height, width, 3), np.float64)
    for j in range(samples):
        for i in range(samples):
            image += ray_pixels(
                spec, (x + (i + 0.5) / samples) / width, (y + (j + 0.5) / samples) / height, width, height
            )[0]
    return (image / (samples * samples)).astype(np.float32)


def motion_reference(
    spec, width=128, height=64, *, angular_speed=240.0, offset=0.0, duration=0.5 / 24, time_samples=128
):
    """Uniform exposure of analytic world rays, independent of Cycles motion."""
    result = np.zeros((height, width, 3), np.float64)
    for index in range(time_samples):
        time = duration * ((index + 0.5) / time_samples - 0.5 + offset)
        sample = dict(spec, angle=spec["angle"] + angular_speed * time)
        result += reference(sample, width, height, samples=4)
    return (result / time_samples).astype(np.float32)


def projected(spec, direction, width, height, angle=0):
    """Independent inverse geometry for stationary-surface vector expectations."""
    a = math.radians(angle)
    c = math.cos(a)
    s = math.sin(a)
    x = c * direction[..., 0] - s * direction[..., 2]
    y = direction[..., 1]
    z = s * direction[..., 0] + c * direction[..., 2]
    model = spec["model"]
    rxy = np.hypot(x, y)
    theta = np.arctan2(rxy, -z)
    if model in ("equirectangular", "central_cylindrical"):
        lo, hi = (
            (spec["longitude_min"], spec["longitude_max"])
            if model == "equirectangular"
            else (spec["u_min"], spec["u_max"])
        )
        lo, hi = math.radians(lo), math.radians(hi)
        phi = np.arctan2(-x, -z)
        centre = -(lo + hi) / 2
        phi += np.round((centre - phi) / (2 * math.pi)) * (2 * math.pi)
        u = (phi + lo) / (lo - hi)
        if model == "equirectangular":
            v = (np.arccos(np.clip(y, -1, 1)) - (math.pi / 2 - math.radians(spec["latitude_min"]))) / math.radians(
                spec["latitude_min"] - spec["latitude_max"]
            )
        else:
            v = (y / np.hypot(x, z) - spec["v_min"] / spec["radius"]) / (
                (spec["v_max"] - spec["v_min"]) / spec["radius"]
            )
    elif model == "equiangular_cubemap_face":
        u = 0.5 + np.arctan2(x, -z) * 2 / math.pi
        v = 0.5 + np.arctan2(y, -z) * 2 / math.pi
    elif model == "mirrorball":
        den = 4 * np.sqrt(np.maximum((1 - z) / 2, 0))
        u = 0.5 + np.divide(x, den, out=np.zeros_like(x), where=den != 0)
        v = 0.5 + np.divide(y, den, out=np.zeros_like(y), where=den != 0)
    else:
        if model == "fisheye_equidistant":
            r = theta / math.radians(spec["fov"])
            sw = sh = 1
        else:
            aspect = width * spec["pixel_aspect"] / height
            horizontal = spec["fit"] == "horizontal" or (spec["fit"] == "auto" and aspect > 1)
            size = spec["sensor_height"] if spec["fit"] == "vertical" else spec["sensor_width"]
            sw, sh = (size, size / aspect) if horizontal else (size * aspect, size)
            if model == "fisheye_equisolid":
                r = 2 * spec["lens"] * np.sin(theta / 2)
            else:
                # Bisection is deliberately independent of the engine's
                # safeguarded Newton implementation. Tested lenses are monotone.
                lower = np.zeros_like(theta)
                upper = np.full_like(theta, np.hypot(sw, sh) * 4)
                for _ in range(60):
                    mid = (lower + upper) / 2
                    value = -sum(k * mid**i for i, k in enumerate(spec["coefficients"]))
                    lower = np.where(value < theta, mid, lower)
                    upper = np.where(value >= theta, mid, upper)
                r = (lower + upper) / 2
        q = np.divide(r, rxy, out=np.zeros_like(r), where=rxy != 0)
        u = 0.5 + x * q / sw
        v = 0.5 + y * q / sh
    return np.stack((u, v), axis=-1)


def motion_vectors(spec, width, height, *, angular_speed=240.0, pixel_offset=(0.5, 0.5)):
    yy, xx = np.indices((height, width))
    d, valid = directions(spec, (xx + pixel_offset[0]) / width, (yy + pixel_offset[1]) / height, width, height)
    centre = projected(spec, d, width, height, spec["angle"])
    pre = projected(spec, d, width, height, spec["angle"] - angular_speed / 24)
    post = projected(spec, d, width, height, spec["angle"] + angular_speed / 24)
    left = pre - centre
    right = centre - post
    if spec["model"] in ("equirectangular", "central_cylindrical"):
        lo, hi = (
            (spec["longitude_min"], spec["longitude_max"])
            if spec["model"] == "equirectangular"
            else (spec["u_min"], spec["u_max"])
        )
        period = 360 / (hi - lo)
        for delta in (left, right):
            delta[..., 0] -= np.round(delta[..., 0] / period) * period
    result = np.concatenate((left, right), axis=-1) * np.array((width, height, width, height))
    return result, valid


def vector_reference(spec, width, height, samples=16):
    """The motion pass averages the nonlinear vector field over each pixel."""
    result = np.zeros((height, width, 4), np.float64)
    for j in range(samples):
        for i in range(samples):
            field, valid = motion_vectors(spec, width, height, pixel_offset=((i + 0.5) / samples, (j + 0.5) / samples))
            result += np.where(valid[..., None], field, 0)
    return result / (samples * samples)


def dof_reference(spec, width=128, height=64, *, focus=3, aperture=0.5, samples=2048):
    """Uniform circular pupil and radial focal surface; no engine camera code."""
    yy, xx = np.indices((height, width), dtype=np.float64)
    result = np.zeros((height, width, 3), np.float64)

    # Deterministic low-discrepancy integration over pixel area and pupil area.
    def radical(index, base):
        value = 0.0
        scale = 1 / base
        while index:
            value += (index % base) * scale
            index //= base
            scale /= base
        return value

    for index in range(1, samples + 1):
        d, valid = directions(spec, (xx + radical(index, 5)) / width, (yy + radical(index, 7)) / height, width, height)
        axis = np.zeros_like(d)
        axis[..., 1] = 1
        axis[np.abs(d[..., 1]) > 0.99] = (1, 0, 0)
        u = np.cross(d, axis)
        u /= np.linalg.norm(u, axis=-1, keepdims=True)
        v = np.cross(d, u)
        radius = aperture * math.sqrt(radical(index, 2))
        angle = 2 * math.pi * radical(index, 3)
        pupil = radius * (u * math.cos(angle) + v * math.sin(angle))
        origin = np.array(spec["origin"]) + pupil
        ray = focus * d - pupil
        ray /= np.linalg.norm(ray, axis=-1, keepdims=True)
        plane = np.where(ray >= 0, 3.0, -3.0)
        distance = np.divide(plane - origin, ray, out=np.full_like(ray, np.inf), where=ray != 0)
        wall = np.argmin(distance, axis=-1)
        sign = np.take_along_axis(ray, wall[..., None], axis=-1)[..., 0] < 0
        result += np.where(valid[..., None], COLORS[wall * 2 + sign], 0)
    return (result / samples).astype(np.float32)
