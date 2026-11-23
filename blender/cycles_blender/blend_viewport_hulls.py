# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
# Budgets and validators: cycles_houdini.blender.blend_viewport (Apache-2.0).
"""Bounded convex viewport previews, computed only in Blender refresh workers."""

from cycles_houdini.blender.blend_viewport import (
    DEFAULT_HULL_VERTICES,
    MAX_PIECES,
    MAX_POINTS,
    MAX_TRIANGLES,
    hull_vertex_limit,
    validate_viewport_hulls,
)


def _convex(points):
    import bmesh

    mesh = bmesh.new()
    try:
        for point in points:
            mesh.verts.new(tuple(float(value) for value in point))
        bmesh.ops.convex_hull(mesh, input=list(mesh.verts), use_existing_faces=False)
        # Interior and unused vertices are not part of the lightweight preview.
        unused = [vertex for vertex in mesh.verts if not vertex.link_faces]
        if unused:
            bmesh.ops.delete(mesh, geom=unused, context="VERTS")
        if not mesh.faces:
            raise ValueError("Evaluated target has no nondegenerate convex surface")
        bmesh.ops.recalc_face_normals(mesh, faces=list(mesh.faces))
        bmesh.ops.triangulate(mesh, faces=list(mesh.faces))
        mesh.verts.index_update()
        mesh.verts.ensure_lookup_table()
        if len(mesh.verts) > MAX_POINTS or len(mesh.faces) > MAX_TRIANGLES:
            raise ValueError("Convex hull exceeds the viewport geometry budget")
        return dict(
            points=[float(v) for vertex in mesh.verts for v in vertex.co],
            triangles=[vertex.index for face in mesh.faces for vertex in face.verts],
        )
    finally:
        mesh.free()


def _preview_points(points, limit):
    """Keep spatially distributed surface samples, including axis extremes.

    This is cosmetic viewport geometry; authoritative render bounds continue to
    use every evaluated vertex. Farthest-point sampling is deterministic and
    avoids a dense curved surface making the entire preview unavailable.
    """
    import numpy as np

    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if not np.isfinite(points).all():
        raise ValueError("Viewport hull has non-finite evaluated vertices")
    if len(points) <= limit:
        return points
    extent = np.ptp(points, axis=0)
    normalized = (points - points.min(axis=0)) / np.where(extent > 0, extent, 1)
    selected = list(dict.fromkeys([*points.argmin(axis=0).tolist(), *points.argmax(axis=0).tolist()]))[:limit]
    distance = np.full(len(points), np.inf)
    for index in selected:
        distance = np.minimum(distance, ((normalized - normalized[index]) ** 2).sum(axis=1))
    while len(selected) < limit:
        index = int(distance.argmax())
        if distance[index] <= 0:
            break
        selected.append(index)
        distance = np.minimum(distance, ((normalized - normalized[index]) ** 2).sum(axis=1))
    return points[selected]


def _build_hulls(manifest, arrays, limit):
    import numpy as np

    count = len(manifest["instances"])
    if not 1 <= count <= MAX_PIECES:
        raise ValueError("Collection exceeds the 128 member viewport hull budget or has no evaluated members")
    local = {}
    # Link hair and point cloud records (blend_export) show the convex hull of their keys or
    # points; a point cloud's samples are grown by its largest radius along the axes, so a
    # cloud of few large points still shows its extent (and a single point a solid).
    clouds = {mesh["key"] for mesh in manifest.get("points", ())}
    for mesh in list(manifest["meshes"]) + list(manifest.get("hair", ())) + list(manifest.get("points", ())):
        points = np.asarray(arrays[mesh["arrays"]["positions"]])
        if not points.size:
            continue  # e.g. the empty own mesh of a geometry-nodes object that outputs points
        points = _preview_points(points, limit)
        if mesh["key"] in clouds and mesh.get("max_radius"):
            offsets = np.concatenate([np.eye(3), -np.eye(3)]) * float(mesh["max_radius"])
            points = _preview_points((points[:, None, :] + offsets[None]).reshape(-1, 3), limit)
        local[mesh["key"]] = _convex(points)
    members, candidates = [], []
    for instance in manifest["instances"]:
        if len(members) >= MAX_PIECES:
            raise ValueError("Collection exceeds the 128 member viewport hull budget")
        hull = local.get(instance["mesh_key"])
        if hull is None:
            continue  # empty geometry has no hull (render bounds skip it too)
        points = np.asarray(hull["points"], dtype=np.float64).reshape(-1, 3)
        matrix = np.asarray(instance["transform"], dtype=np.float64).reshape(4, 4)
        points = points @ matrix[:3, :3].T + matrix[:3, 3]
        triangles = list(hull["triangles"])
        if np.linalg.det(matrix[:3, :3]) < 0:
            triangles = [triangles[i + j] for i in range(0, len(triangles), 3) for j in (0, 2, 1)]
        members.append(dict(points=points.reshape(-1).tolist(), triangles=triangles))
        candidates.extend(points.tolist())
        if sum(len(p["points"]) for p in members) > MAX_POINTS * 3:
            raise ValueError("Collection member hulls exceed the viewport geometry budget")
    if not members:
        raise ValueError("Evaluated target has no nondegenerate convex surface")
    whole = _convex(_preview_points(candidates, limit))
    result = dict(whole=whole, members=members, max_vertices=limit)
    if not validate_viewport_hulls(result):
        raise ValueError("Convex hull preview exceeds its bounded saved metadata budget")
    return result


def build_viewport_hulls(manifest, arrays, max_vertices=DEFAULT_HULL_VERTICES):
    """Apply the same requested cap independently to every object/member hull.

    Geometry and saved metadata guards refuse an oversized selection explicitly;
    they never silently reduce a collection member's requested detail.
    """
    limit = hull_vertex_limit(max_vertices)
    result = _build_hulls(manifest, arrays, limit)
    result["reduced"] = any(
        len(arrays[mesh["arrays"]["positions"]]) > limit
        for mesh in list(manifest["meshes"]) + list(manifest.get("hair", ())) + list(manifest.get("points", ()))
    )
    if not validate_viewport_hulls(result):
        raise ValueError(
            "Requested hull detail exceeds the 16384 vertex, 32768 triangle or 2 MiB selection budget; lower Maximum Hull Vertices or split the selection"
        )
    return result
