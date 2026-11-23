# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Conservative Blender Subsurf to native Cycles control-mesh contract.

Blender's limit-surface modifier uses a (2**render_levels + 1) grid.
The native fixed-level dicer uses that same parameter spacing, independently
of the camera. Its OpenSubdiv isolation is pinned to three. Unsupported
semantics deliberately keep Blender's fully evaluated mesh.
"""

import numpy as np


UV_POLICIES = {
    "NONE": "all",
    "PRESERVE_CORNERS": "corners_only",
    "PRESERVE_CORNERS_AND_JUNCTIONS": "corners_plus1",
    "PRESERVE_CORNERS_JUNCTIONS_AND_CONCAVE": "corners_plus2",
    "PRESERVE_BOUNDARIES": "boundaries",
    "SMOOTH_ALL": "none",
}


class EvaluatedFallback(Exception):
    def __init__(self, identity, reason):
        self.identity, self.reason = identity, reason
        super().__init__(reason)


def _action_paths(action):
    if action is None:
        return
    for curve in getattr(action, "fcurves", ()):
        yield curve.data_path
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                for curve in bag.fcurves:
                    yield curve.data_path


def _animated_modifiers(obj):
    animation = obj.animation_data
    if animation is None:
        return False
    actions = [animation.action]
    for track in animation.nla_tracks:
        actions.extend(strip.action for strip in track.strips)
    return any(path.startswith("modifiers[") for action in actions for path in _action_paths(action))


def has_geometry_consumers(bpy, obj, users):
    """Include Collection Info consumers reached through containing collections."""
    pending, visited = [obj], set()
    while pending:
        block = pending.pop()
        if block in visited:
            continue
        visited.add(block)
        for user in users.get(block, ()):
            if isinstance(user, bpy.types.Scene):
                continue
            if isinstance(user, bpy.types.Collection):
                pending.append(user)
                continue
            if (
                isinstance(block, bpy.types.Collection)
                and isinstance(user, bpy.types.Object)
                and user.instance_type == "COLLECTION"
                and user.instance_collection == block
            ):
                # Ordinary collection instances keep the original prototype
                # object and export its native subdivision settings separately.
                continue
            return True
    return False


def settings(obj, scene):
    """Return the last render-enabled modifier and its representable settings."""
    enabled = [modifier for modifier in obj.modifiers if modifier.show_render]
    if not enabled or enabled[-1].type != "SUBSURF":
        return None, None, None
    modifier = enabled[-1]
    reason = None
    level = int(modifier.render_levels)
    if scene.render.use_simplify:
        level = min(level, int(scene.render.simplify_subdivision_render))
    if obj.type != "MESH":
        reason = "Only mesh Subsurf control surfaces are supported"
    elif obj.library is not None:
        reason = "Linked modifier state requires unchanged Blender evaluation"
    elif modifier.subdivision_type != "CATMULL_CLARK":
        reason = "Simple subdivision requires evaluated mesh normals"
    elif not modifier.use_limit_surface or modifier.quality != 3:
        reason = "Native limit patches require Use Limit Surface and Quality 3"
    elif modifier.use_custom_normals:
        reason = "Custom-normal interpolation requires Blender evaluation"
    elif not 1 <= level <= 12:
        reason = "Effective render subdivision level must be between 1 and 12"
    elif modifier.uv_smooth not in UV_POLICIES or modifier.boundary_smooth not in ("ALL", "PRESERVE_CORNERS"):
        reason = "Unsupported subdivision boundary or UV interpolation policy"
    elif obj.animation_data and obj.animation_data.drivers:
        reason = "Driven object state requires unchanged Blender evaluation"
    elif _animated_modifiers(obj):
        reason = "Animated modifier state requires unchanged Blender evaluation"
    config = dict(
        type="catmull_clark",
        level=level,
        adaptive=False,
        boundary="edge_only" if modifier.boundary_smooth == "ALL" else "edge_and_corner",
        fvar=UV_POLICIES.get(modifier.uv_smooth),
        use_creases=bool(modifier.use_creases),
        modifier=modifier.name,
    )
    return modifier, config, reason


def validate(mesh, record, arrays, config):
    """Check evaluated pre-Subsurf data, before publishing any native bundle."""
    fields = record["arrays"]
    counts = arrays[fields["polygon_counts"]]
    indices = arrays[fields["vertex_indices"]]
    if not len(counts) or np.any(counts != 4):
        return "Native Blender subdivision currently requires quad control faces"
    if not np.all(arrays[fields["smooth"]]) or mesh.has_custom_normals:
        return "Flat faces or custom normals require Blender evaluation"
    sharp = mesh.attributes.get("sharp_edge")
    if sharp and any(value.value for value in sharp.data):
        return "Split sharp-edge normals require Blender evaluation"
    edges, vertex_faces = {}, [set() for _ in mesh.vertices]
    for face, corners in enumerate(indices.reshape(-1, 4)):
        if len(set(map(int, corners))) != 4:
            return "Repeated control vertices require Blender topology evaluation"
        for a, b in zip(corners, np.roll(corners, -1)):
            a, b = int(a), int(b)
            vertex_faces[a].add(face)
            key = tuple(sorted((a, b)))
            edges.setdefault(key, []).append((face, a < b))
    if any(len(uses) != 2 or uses[0][1] == uses[1][1] for uses in edges.values()):
        return "Open, nonmanifold or inconsistently oriented topology requires Blender evaluation"
    # Reject bow-tie vertices and loose geometry as well as nonmanifold edges.
    neighbours = [dict() for _ in mesh.vertices]
    for (a, b), uses in edges.items():
        left, right = uses[0][0], uses[1][0]
        for vertex in (a, b):
            neighbours[vertex].setdefault(left, set()).add(right)
            neighbours[vertex].setdefault(right, set()).add(left)
    for vertex, faces in enumerate(vertex_faces):
        if not faces:
            return "Loose control vertices require Blender evaluation"
        pending, visited = [next(iter(faces))], set()
        while pending:
            face = pending.pop()
            if face not in visited:
                visited.add(face)
                pending.extend(neighbours[vertex].get(face, ()))
        if visited != faces:
            return "Nonmanifold control vertex requires Blender evaluation"
    if len(edges) != len(mesh.edges):
        return "Loose control edges require Blender evaluation"
    # Match the native conservative signed-indexing guard before handing off.
    if len(counts) > (2**31 - 1) // (16 << (2 * config["level"])):
        return "Control mesh and subdivision level exceed native indexing limits"
    if record["color_attributes"]:
        return "Color subdivision interpolation requires Blender evaluation"
    builtins = {
        "crease_edge",
        "crease_vert",
        "bevel_weight_edge",
        "bevel_weight_vert",
        "sharp_face",
        "sharp_edge",
        "material_index",
    }
    if any(field["name"] not in builtins for field in record["attributes"]):
        return "Named attribute subdivision interpolation requires Blender evaluation"
    # Blender merges nearby UVs with a tolerance while Cycles merges equal
    # values. Avoid changing an almost-connected UV seam into a separate seam.
    for layer in record["uv_layers"]:
        values = arrays[layer["array"]]
        uv_faces = values.reshape(-1, 4, 2)
        winding = np.sign(
            np.sum(
                uv_faces[:, :, 0] * np.roll(uv_faces[:, :, 1], -1, axis=1)
                - uv_faces[:, :, 1] * np.roll(uv_faces[:, :, 0], -1, axis=1),
                axis=1,
            )
        )
        groups = {}
        for corner, (point, uv) in enumerate(zip(indices, values)):
            group = groups.setdefault(int(point), [])
            if any(not np.array_equal(uv, other) and np.all(np.abs(uv - other) < 1e-4) for other, sign in group):
                return "Near-coincident UV seams require Blender evaluation"
            sign = winding[corner // 4]
            if any(np.array_equal(uv, other) and sign != other_sign for other, other_sign in group):
                return "Opposite UV winding at a shared value requires Blender evaluation"
            group.append((uv, sign))
    if config["use_creases"]:
        for name, domain in (("crease_edge", "EDGE"), ("crease_vert", "POINT")):
            field = next((field for field in record["attributes"] if field["name"] == name), None)
            if field:
                values = arrays[field["array"]]
                if field["domain"] != domain or np.any((values != 0) & (values != 1)):
                    return "Fractional or unsupported crease weights require Blender evaluation"
    return None
