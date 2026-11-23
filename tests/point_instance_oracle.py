# SPDX-License-Identifier: Apache-2.0
"""Independent oracle for Houdini point-instance transforms.

Pure Python, no Houdini: implements SideFX's documented composition ("Copying and instancing
point attributes") and the Mantra-verified Instance OBJ order. Row-vector 4x4 matrices
(Houdini convention): p_world = p_local * M.

  transform present:  X * M * T
  orient present:     X * S * (O*R) * T
  otherwise:          X * S * L * R * T
  X = translate(-pivot), S = scale(scale*pscale), T = translate(P + trans),
  L = lookat(N or v, up) / dihedral(+Z, N or v), R = rot.
  Instance OBJ world = prototype.world * point * instancer.world  (Mantra probe 3)
"""

import math


def ident():
    return [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


def mul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def translate(t):
    m = ident()
    m[3][0], m[3][1], m[3][2] = t
    return m


def scale(s):
    m = ident()
    m[0][0], m[1][1], m[2][2] = s
    return m


def embed3(m3):
    m = ident()
    for r in range(3):
        for c in range(3):
            m[r][c] = m3[r][c]
    return m


def quat(q):
    """Houdini vector4 quaternion (x, y, z, w) -> row-vector rotation matrix."""
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n == 0:
        return ident()
    x, y, z, w = x / n, y / n, z / n, w / n
    col = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return embed3([[col[c][r] for c in range(3)] for r in range(3)])  # transpose: row vectors


def _norm(v):
    l = math.sqrt(sum(x * x for x in v))
    return [x / l for x in v] if l else None


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def lookat(direction, up):
    z = _norm(direction)
    x = _norm(_cross(up, z))
    if z is None or x is None:
        return None
    y = _cross(z, x)
    return embed3([x, y, z])


def dihedral(direction):
    """Minimal rotation taking +Z to `direction` (row-vector form)."""
    n = _norm(direction)
    if n is None:
        return ident()
    c = n[2]
    k = _cross([0, 0, 1], n)
    s = math.sqrt(sum(v * v for v in k))
    if s < 1e-12:
        return ident() if c > 0 else embed3([[1, 0, 0], [0, -1, 0], [0, 0, -1]])  # 180 deg about X (verify vs Houdini)
    k = [v / s for v in k]
    K = [[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]]
    K2 = [[sum(K[r][i] * K[i][cc] for i in range(3)) for cc in range(3)] for r in range(3)]
    col = [[(1 if r == cc else 0) + s * K[r][cc] + (1 - c) * K2[r][cc] for cc in range(3)] for r in range(3)]
    return embed3([[col[cc][r] for cc in range(3)] for r in range(3)])


def point_matrix(a):
    """a: dict of point attributes (P required; others optional, Houdini names)."""
    P = a.get("P", (0, 0, 0))
    trans = a.get("trans", (0, 0, 0))
    pivot = a.get("pivot", (0, 0, 0))
    X = translate([-v for v in pivot])
    T = translate([p + t for p, t in zip(P, trans)])
    if "transform" in a:
        M = a["transform"]
        M = embed3(M) if len(M) == 3 else [list(r) for r in M]
        return mul(mul(X, M), T)
    pscale = a.get("pscale", 1.0)
    s3 = a.get("scale", (1, 1, 1))
    S = scale([pscale * v for v in s3])
    R = quat(a["rot"]) if "rot" in a else ident()
    if "orient" in a:
        return mul(mul(mul(X, S), mul(quat(a["orient"]), R)), T)
    direction = a.get("N", a.get("v"))
    L = ident()
    if direction is not None:
        up = a.get("up")
        L = (lookat(direction, up) if up is not None and any(up) else None) or dihedral(direction)
    return mul(mul(mul(mul(X, S), L), R), T)


def instance_world(prototype_world, point, instancer_world):
    return mul(mul(prototype_world, point), instancer_world)


def max_error(a, b):
    return max(abs(a[r][c] - b[r][c]) for r in range(4) for c in range(4))


if __name__ == "__main__":
    # Self-check against Houdini's Copy to Points (research/probe-2/c2p_oracle.json).
    import json, sys

    cases = {
        "none": {},
        "N": {"N": (1, 1, 0)},
        "N_up": {"N": (1, 1, 0), "up": (0, 0, 1)},
        "v": {"v": (0, 0, 3)},
        "v_up": {"v": (1, 0, 0), "up": (0, 1, 1)},
        "N_v": {"N": (1, 0, 0), "v": (0, 1, 0)},
        "orient": {"orient": None},
        "pscale_scale": {"pscale": 2, "scale": (1, 3, 0.5)},
        "neg_scale": {"scale": (-1, 2, 1)},
    }

    def axis_angle(deg, axis):
        h = math.radians(deg) / 2
        n = _norm(axis)
        return (n[0] * math.sin(h), n[1] * math.sin(h), n[2] * math.sin(h), math.cos(h))

    cases["orient"] = {"orient": axis_angle(30, (1, 2, 3))}
    cases["orient_N"] = {"orient": axis_angle(30, (1, 2, 3)), "N": (1, 0, 0)}
    cases["orient_rot"] = {"orient": axis_angle(30, (0, 0, 1)), "rot": axis_angle(45, (1, 0, 0))}
    cases["N_rot"] = {"N": (0, 1, 0), "rot": axis_angle(45, (1, 0, 0))}
    cases["pivot"] = {"pivot": (0.5, 0.2, 0), "pscale": 2, "orient": axis_angle(90, (0, 1, 0))}
    cases["trans"] = {"trans": (0, 0, 4), "orient": axis_angle(90, (0, 1, 0))}
    c40, s40 = math.cos(math.radians(40)), math.sin(math.radians(40))
    rot40 = [[c40, s40, 0], [-s40, c40, 0], [0, 0, 1]]
    cases["xform3"] = {
        "transform": [[2 * c40, s40, 0], [-2 * s40, c40, 0], [0, 0, 1]]
    }  # VEX rotate() then scale(): R*S
    cases["xform3_pscale"] = {"transform": rot40, "pscale": 3, "scale": (1, 2, 1)}
    cases["xform3_orient"] = {"transform": rot40, "orient": axis_angle(90, (0, 1, 0))}
    m4 = mul(translate((0, 7, 0)), embed3(rot40))
    cases["xform4_trans"] = {"transform": m4, "trans": (1, 0, 0), "pivot": (0, 1, 0)}
    cases["all"] = {
        "N": (1, 0, 0),
        "up": (0, 1, 0),
        "orient": axis_angle(20, (0, 1, 0)),
        "rot": axis_angle(10, (1, 0, 0)),
        "pscale": 1.5,
        "scale": (1, 2, 3),
        "trans": (0, 1, 0),
        "pivot": (0.1, 0, 0),
    }
    reference = json.load(open(sys.argv[1]))
    worst = 0
    for name, attrs in cases.items():
        expected = reference[name]["full"]
        got = point_matrix(dict(attrs, P=(2, 3, 4)))
        err = max_error(got, expected)
        worst = max(worst, err)
        print(f"{name:14s} max|d| = {err:.2e}" + ("" if err < 1e-5 else "   <-- MISMATCH"))
    print("worst", worst)
