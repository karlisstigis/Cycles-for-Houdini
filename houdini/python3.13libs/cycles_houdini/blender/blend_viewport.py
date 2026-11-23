# SPDX-License-Identifier: Apache-2.0
"""Bounded convex viewport previews: their budgets and the validators both sides use.

The hulls are computed only in Blender refresh workers, by
blender/cycles_blender/blend_viewport_hulls.py (GPL-3.0-or-later, runs inside
Blender). Houdini validates the saved previews with this module, which needs no
Blender.
"""

import json
import math

DEFAULT_HULL_VERTICES = 64
MAX_HULL_VERTICES = 4096
MAX_POINTS = 16384
MAX_TRIANGLES = 32768
MAX_PIECES = 128
MAX_BYTES = 2 * 1024 * 1024


def validate_piece(piece):
    if not isinstance(piece, dict):
        return False
    points, triangles = piece.get("points"), piece.get("triangles")
    return (
        isinstance(points, list)
        and 9 <= len(points) <= MAX_POINTS * 3
        and len(points) % 3 == 0
        and all(type(v) in (int, float) and math.isfinite(v) for v in points)
        and isinstance(triangles, list)
        and 3 <= len(triangles) <= MAX_TRIANGLES * 3
        and len(triangles) % 3 == 0
        and all(type(v) is int and 0 <= v < len(points) // 3 for v in triangles)
    )


def validate_viewport_hulls(value):
    if not isinstance(value, dict) or not validate_piece(value.get("whole")):
        return False
    members = value.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_PIECES or not all(map(validate_piece, members)):
        return False
    pieces = [value["whole"], *members]
    return (
        sum(len(p["points"]) for p in pieces) <= MAX_POINTS * 3
        and sum(len(p["triangles"]) for p in pieces) <= MAX_TRIANGLES * 3
        and len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode()) <= MAX_BYTES
    )


def hull_vertex_limit(value):
    """The slider is a convenience; explicit values retain one documented guard."""
    if type(value) is not int or not 8 <= value <= MAX_HULL_VERTICES:
        raise ValueError(f"Maximum Hull Vertices must be an integer from 8 to {MAX_HULL_VERTICES}")
    return value
