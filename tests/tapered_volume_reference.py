# SPDX-License-Identifier: Apache-2.0
"""Independent finite-frustum support, centre interpolation and optical integrals."""

import math
import numpy as np


def matrix(field):
    return np.asarray(field["matrix"], dtype=np.float64).reshape(4, 4)


def forward(unit, taper):
    q = 2 * np.asarray(unit, dtype=np.float64) - 1
    q = q.copy()
    q[..., :2] *= 1 + (np.asarray(taper) - 1) * (1 - q[..., 2, None]) * 0.5
    return q


def values(unit, resolution, field, level=0.35, border=2):
    # SDK trilinear interpolation with its observed constant/streak border.
    # Physical support is clipped separately, so border sampling cannot add mass.
    raw = np.asarray(unit)
    res = np.asarray(resolution)
    unit = np.clip(raw, 0.5 / res, 1 - 0.5 / res)
    if border == 0:
        weight = np.prod(np.minimum(1, np.maximum(0, np.minimum(raw * res + 0.5, (1 - raw) * res + 0.5))), axis=-1)
    else:
        assert border == 2, border
        weight = np.ones(unit.shape[:-1])
    if field in ("density", "float_reference"):
        return level * (1 + 0.2 * unit[..., 0] + 0.1 * unit[..., 1] + 0.15 * unit[..., 2]) * weight
    if field in ("integer_auxiliary", "integer_density"):
        return np.where(weight >= 1, 1.0, 0.0)
    assert field == "tint", field
    return (np.array((0.18, 0.36, 0.62)) + unit * np.array((0.12, 0.08, 0.10))) * weight[..., None]


def target_plan(resolution, taper):
    extent = np.r_[np.maximum(1, np.abs(taper)), 1.0]
    narrow = np.r_[np.where(np.asarray(taper) > 0, np.minimum(1, taper), 1), 1.0]
    target = np.ceil(2 * np.asarray(resolution) * extent / narrow)
    usable = bool(np.isfinite(target).all() and np.all(target <= 2048) and np.prod(target) <= 64 * 1024 * 1024)
    return extent, target.astype(np.int64), usable


def numerical(field, *, level=0.35, integer_density=False):
    """Reference sums/first sample from authored field values, never adapter output."""
    resolution = np.asarray(field["resolution"])
    taper = np.asarray(field["taper"])
    extent, target, usable = target_plan(resolution, taper)
    assert usable
    z, y, x = np.meshgrid(
        *[(-e + (np.arange(n) + 0.5) * 2 * e / n) for e, n in zip(extent[::-1], target[::-1])], indexing="ij"
    )
    q = np.stack((x, y, z), axis=-1)
    width = 1 + (taper - 1) * (1 - q[..., 2, None]) * 0.5
    with np.errstate(divide="ignore", invalid="ignore"):
        unit = np.concatenate((q[..., :2] / width, q[..., 2, None]), axis=-1) * 0.5 + 0.5
    valid = np.isfinite(unit).all(-1) & np.all((unit >= 0) & (unit <= 1), axis=-1) & np.all(width != 0, axis=-1)
    kind = "integer_density" if integer_density else field["name"]
    v = values(np.nan_to_num(unit), resolution, kind, level, field.get("border", 2))
    if v.ndim == valid.ndim:
        v = v[..., None]
    v = np.where(valid[..., None], v, 0)
    basis = matrix(field)
    origin = np.r_[-extent + extent / target, 1] @ basis
    sums = v.sum(axis=(0, 1, 2))
    zero = v[0, 0, 0]
    if len(sums) == 1:
        sums = np.r_[sums, 0, 0]
        zero = np.r_[zero, 0, 0]
    return dict(
        target=target.tolist(),
        active_voxels=int(np.count_nonzero(np.any(v != 0, axis=-1))),
        active_sums=sums.tolist(),
        voxel_zero=zero.tolist(),
        origin=origin[:3].tolist(),
        voxel_size=(np.linalg.norm(basis[:3, :3], axis=1) * 2 * extent / target).tolist(),
    )


def assert_centres(field):
    m = matrix(field)
    res = np.asarray(field["resolution"])
    errors = []
    for sample in field["centres"]:
        unit = (np.asarray(sample["index"]) + 0.5) / res
        expected = np.r_[forward(unit, field["taper"]), 1] @ m
        error = float(np.max(np.abs(expected[:3] - sample["position"])))
        assert error < 3e-6, (field["name"], sample, error)
        errors.append(error)
    return max(errors)


def _segments(origin, direction, taper):
    """Intersect a ray with each convex section of a signed frustum/hourglass."""
    bounds = [-1.0, 1.0]
    for t in taper:
        if t != 1:
            root = (t + 1) / (t - 1)
            if -1 < root < 1:
                bounds.append(root)
    bounds = sorted(set(bounds))
    result = []
    for lo, hi in zip(bounds, bounds[1:]):
        middle = (lo + hi) * 0.5
        sign = np.sign(1 + (np.asarray(taper) - 1) * (1 - middle) * 0.5)
        a = (1 + np.asarray(taper)) * 0.5
        b = (1 - np.asarray(taper)) * 0.5
        normals = [(np.array((0, 0, 1)), hi), (np.array((0, 0, -1)), -lo)]
        for axis in (0, 1):
            for s in (-1, 1):
                n = np.zeros(3)
                n[axis] = s
                n[2] = -sign[axis] * b[axis]
                normals.append((n, sign[axis] * a[axis]))
        lower = np.zeros(origin.shape[:-1])
        upper = np.full_like(lower, 20.0)
        for n, bound in normals:
            denominator = direction @ n
            numerator = bound - origin @ n
            if abs(denominator) < 1e-12:
                upper = np.where(numerator >= -1e-12, upper, -1)
            elif denominator > 0:
                upper = np.minimum(upper, numerator / denominator)
            else:
                lower = np.maximum(lower, numerator / denominator)
        result.append((lower, np.maximum(lower, upper)))
    return result


def image(field, *, width=96, height=64, camera_x=0, level=0.35, integer=False, emission=False, absent=False, spp=3):
    """Gaussian optical integral over the actual finite native support.

    Pixel sub-samples use a symmetric box rule. Renderer filtering and a finite
    resampled boundary are measured against this independent continuous target.
    """
    offsets = (np.arange(spp) + 0.5) / spp
    yy, xx, sy, sx = np.meshgrid(np.arange(height), np.arange(width), offsets, offsets, indexing="ij")
    world = np.stack(
        (
            (xx + sx) / width * 6 - 3 + camera_x,
            (yy + sy) / height * 6 * height / width - 3 * height / width,
            np.full_like(xx, 5.0),
        ),
        axis=-1,
    )
    inverse = np.linalg.inv(matrix(field))
    origin = np.concatenate((world, np.ones(world.shape[:-1] + (1,))), axis=-1) @ inverse
    direction = np.r_[0, 0, -1, 0] @ inverse
    origin = origin[..., :3]
    direction = direction[:3]
    integral = np.zeros(world.shape[:-1] + ((3,) if emission else ()))
    nodes, weights = np.polynomial.legendre.leggauss(48)
    if not absent:
        for lower, upper in _segments(origin, direction, np.asarray(field["taper"])):
            # Loop quadrature nodes to bound memory independently of image size.
            length = upper - lower
            for node, weight in zip(nodes, weights):
                t = (upper + lower) * 0.5 + length * 0.5 * node
                q = origin + t[..., None] * direction
                factor = 1 + (np.asarray(field["taper"]) - 1) * (1 - q[..., 2, None]) * 0.5
                with np.errstate(divide="ignore", invalid="ignore"):
                    unit = np.concatenate((q[..., :2] / factor, q[..., 2, None]), axis=-1) * 0.5 + 0.5
                v = values(
                    np.nan_to_num(unit),
                    field["resolution"],
                    "tint" if emission else "integer_density" if integer else "density",
                    level,
                    field.get("border", 2),
                )
                contribution = length * 0.5 * weight
                integral += v * contribution[..., None] if emission else v * contribution
    result = 1 + integral if emission else np.exp(-integral * (0.42 if integer else 1))[..., None] * np.ones(3)
    return result.mean(axis=(2, 3)).astype(np.float32)
