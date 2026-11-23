# SPDX-License-Identifier: Apache-2.0
"""Independent authored projection and colored-box ray integration."""

import math
import numpy as np

COLORS = np.array(
    ((0.8, 0.1, 0.1), (0.1, 0.8, 0.1), (0.1, 0.1, 0.8), (0.8, 0.8, 0.1), (0.8, 0.1, 0.8), (0.1, 0.8, 0.8))
)
DURATION = 0.5 / 24


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


def authored(case, t, amplitude=1):
    # Kept as explicit authored equations, not captured samples or engine
    # interpolation. Every consumed quantity is affine on the fixture's grid.
    s = dict(
        model="equirectangular",
        fov=180.0,
        lens=10.5,
        latitude_min=-60 + 240 * t,
        latitude_max=60 + 480 * t,
        longitude_min=-180 + 1800 * t,
        longitude_max=180 + 3600 * t,
        u_min=-120 + 1200 * t,
        u_max=120 + 2400 * t,
        v_min=-1 + 12 * t,
        v_max=1 + 24 * t,
        radius=1,
        fit="horizontal",
        sensor_width=36 + 480 * t,
        sensor_height=24,
        coefficients=(0, -0.05 - 0.48 * t, 0, 0.0001 * t, 0),
        pixel_aspect=1,
        angle=0,
        origin=(0, 0, 0),
        near=0.01,
        far=1000,
        tangent=0.6 + 19.2 * t * amplitude,
        orthowidth=8 + 192 * t,
    )
    if case in ("perspective", "extra_fov", "clipping", "motion_off"):
        s["model"] = "perspective"
    if case == "extra_fov":
        q = np.clip(t / DURATION + 0.5, 0, 1) * 4
        wave = -q if q < 1 else q - 2 if q < 3 else 4 - q
        s["tangent"] = 0.6 + 0.15 * amplitude * wave
    if case in ("orthographic", "ortho_clip", "bad_width"):
        s["model"] = "orthographic"
    if case in ("clipping", "projection_hold"):
        s["near"] = 3 + 96 * t
    if case == "ortho_clip":
        s.update(orthowidth=8, far=3 + 96 * t)
    if case == "bad_width":
        s["orthowidth"] = 8
    if case in ("fisheye_equidistant", "fisheye_equisolid", "bad_lens", "bad_sensor", "sensor", "fit_hold"):
        s.update(
            model="fisheye_equidistant" if case == "fisheye_equidistant" else "fisheye_equisolid",
            fov=210 + 2400 * t,
            lens=10.5 + 96 * t,
        )
    if case == "sensor":
        s.update(fit="vertical", sensor_height=24 + 480 * t)
    if case == "bad_lens":
        s["lens"] = 10.5
    if case == "bad_sensor":
        s["sensor_width"] = 36
    if case == "polynomial":
        s["model"] = "fisheye_lens_polynomial"
    if case in ("cylinder", "cylinder_radius"):
        s["model"] = "central_cylindrical"
    if case == "cylinder_radius":
        s.update(v_min=-1, v_max=1, radius=1 / (1 + 12 * t))
    if case in ("bad_range", "dof_independent"):
        s.update(longitude_min=-180, longitude_max=180)
    if case == "projection_hold":
        s.update(latitude_min=-60, latitude_max=60, longitude_min=-180, longitude_max=180)
    return s


def exposure_reference(
    case, width=128, height=64, *, offset=0, amplitude=1, pixel_aspect=1, now=0, blur=True, time_samples=96, spatial=4
):
    yy, xx = np.indices((height, width), dtype=float)
    result = np.zeros((height, width, 3))
    for step in range(time_samples if blur else 1):
        dt = DURATION * ((step + 0.5) / time_samples - 0.5 + offset / 2) if blur else 0
        spec = authored(case, dt, amplitude)
        spec["pixel_aspect"] = pixel_aspect
        for j in range(spatial):
            for i in range(spatial):
                u = (xx + (i + 0.5) / spatial) / width
                v = (yy + (j + 0.5) / spatial) / height
                origin = np.zeros((height, width, 3))
                origin[:, :, 0] = 3.9 * (now + dt)
                if spec["model"] == "perspective":
                    ratio = width / height
                    effective = ratio * pixel_aspect
                    tangent = spec["tangent"] * max(1, ratio) / max(1, effective)
                    d = np.stack(
                        (
                            (2 * u - 1) * max(1, effective) * tangent,
                            (2 * v - 1) / max(1, 1 / effective) * tangent,
                            -np.ones_like(u),
                        ),
                        axis=-1,
                    )
                    d /= np.linalg.norm(d, axis=-1, keepdims=True)
                    valid = np.ones_like(u, dtype=bool)
                    near = spec["near"] / (-d[:, :, 2])
                    far = spec["far"] / (-d[:, :, 2])
                elif spec["model"] == "orthographic":
                    origin[:, :, 0] += (u - 0.5) * spec["orthowidth"]
                    origin[:, :, 1] += (v - 0.5) * spec["orthowidth"] / (width / height) / pixel_aspect
                    d = np.zeros_like(origin)
                    d[:, :, 2] = -1
                    valid = np.ones_like(u, dtype=bool)
                    near = spec["near"]
                    far = spec["far"]
                else:
                    d, valid = directions(spec, u, v, width, height)
                    near = spec["near"]
                    far = spec["far"]
                # Every ray begins inside the colored box in the panoramic
                # cases; orthographic rays outside the box have no forward hit.
                distance = np.divide(np.where(d >= 0, 3.0, -3.0) - origin, d, out=np.full_like(d, np.inf), where=d != 0)
                distance = np.where(distance > 0, distance, np.inf)
                axis = np.argmin(distance, axis=-1)
                hit = np.take_along_axis(distance, axis[:, :, None], axis=-1)[:, :, 0]
                point = origin + d * np.where(np.isfinite(hit), hit, 0)[:, :, None]
                valid &= np.isfinite(hit) & (hit >= near) & (hit <= far) & np.all(np.abs(point) <= 3.0000001, axis=-1)
                sign = np.take_along_axis(d, axis[:, :, None], axis=-1)[:, :, 0] < 0
                result += np.where(valid[:, :, None], COLORS[axis * 2 + sign], 0)
    return (result / ((time_samples if blur else 1) * spatial * spatial)).astype(np.float32)
