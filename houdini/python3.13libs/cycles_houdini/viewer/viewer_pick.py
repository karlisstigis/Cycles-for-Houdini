# SPDX-License-Identifier: Apache-2.0
"""Render viewer pick tools: the object and material under a rendered pixel, and pixel values.

Identity comes from the renderer's own camera hit: the world position that Pick
Focus already samples (position and depth planes). The rendered objects of the
ROP are then intersected along the camera ray through that point, in Houdini,
and the one whose surface lies closest to the rendered position owns the pixel.
No render pass is added and the scene is not changed.
"""

import math
import struct

import hou

MATERIAL_ATTRIBUTE = "shop_materialpath"
# Packed nesting followed when looking for embedded material assignments.
MAX_PACKED_DEPTH = 8


class Hit:
    """The object owning a rendered pixel, and the primitive chain that was hit."""

    def __init__(self, owner, error, levels, prototype=None):
        self.owner = owner
        self.error = error
        # Material candidates from the innermost primitive outwards: (geometry, prim or None).
        self.levels = levels
        self.prototype = prototype


def _vector(value):
    return hou.Vector3(value[0], value[1], value[2])


def ray_origin(camera, position):
    """World-space origin of the camera ray that reached position."""
    transform = camera.worldTransform()
    origin = hou.Vector3(0, 0, 0) * transform
    if _orthographic(camera):
        forward = (hou.Vector3(0, 0, -1) * transform - origin).normalized()
        depth = (position - origin).dot(forward)
        return position - forward * max(depth, 1.0)
    return origin


def _orthographic(camera):
    projection = camera.parm("projection")
    return projection is not None and projection.evalAsString() in ("ortho", "orthographic")


def camera_ray(camera, x, y, width, height, pixel_aspect=1.0, now=None):
    """(origin, direction) in world space through the centre of pixel (x, y from the bottom).

    Houdini camera conventions: horizontal aperture, screen window offset and size in
    NDC units. Panoramic Cycles lenses are not modelled: returns None for them.
    """
    now = hou.time() if now is None else now
    if camera.parm("cycles_projection") and camera.parm("cycles_projection").evalAtTime(now) == "panorama":
        return None

    def value(name, default):
        parm = camera.parm(name)
        return parm.evalAtTime(now) if parm is not None else default

    u = (x + 0.5) / width - 0.5
    v = (y + 0.5) / height - 0.5
    sx = u * value("winsizex", 1.0) + value("winx", 0.0)
    sy = v * value("winsizey", 1.0) + value("winy", 0.0)
    vertical = height / (width * (pixel_aspect or 1.0))
    transform = camera.worldTransformAtTime(now)
    if _orthographic(camera):
        span = value("orthowidth", 2.0)
        local_origin = hou.Vector3(sx * span, sy * span * vertical, 0.0)
        local_direction = hou.Vector3(0.0, 0.0, -1.0)
    else:
        scale = value("aperture", 41.4214) / value("focal", 50.0)
        local_origin = hou.Vector3(0.0, 0.0, 0.0)
        local_direction = hou.Vector3(sx * scale, sy * scale * vertical, -1.0)
    origin = local_origin * transform
    direction = (local_origin + local_direction) * transform - origin
    return origin, direction.normalized()


def rendered_objects(rop, now):
    """(object, render SOP) pairs the ROP renders, camera-visible only."""
    from ..scene import object_scene, native_scene

    if object_scene.enabled(rop, now):
        pairs = [(obj, obj.renderNode()) for obj in object_scene.selected(rop, now)]
    else:
        path = native_scene.source_path(rop, "soppath", now)
        sop = hou.node(path) if path else None
        pairs = [(native_scene.object_for_sop(sop), sop)] if sop is not None else []
    result = []
    for obj, sop in pairs:
        if obj is None or sop is None:
            continue
        try:
            if not object_scene.object_visibility(obj, now) & 1:
                continue
        except hou.Error:
            pass
        result.append((obj, sop))
    return result


def _geometry(sop, now):
    try:
        geometry = sop.geometryAtFrame(hou.timeToFrame(now)) if hasattr(sop, "geometryAtFrame") else sop.geometry()
    except hou.Error:
        return None
    return geometry


def _intersect(geometry, origin, direction, target=None):
    """(prim or None, local hit position) along a local ray, or None.

    With target (the renderer's own hit), primitives the ray misses (points rendered
    as spheres, curves, volumes) are matched by their distance to target instead.
    """
    position, normal, uvw = hou.Vector3(), hou.Vector3(), hou.Vector3()
    reach = (target - origin).length() * 4 + 1 if target is not None else 1e18
    number = geometry.intersect(origin, direction, position, normal, uvw, min_hit=0.0, max_hit=reach)
    best = (geometry.prim(number), position) if number >= 0 else None
    if target is None:
        return best
    error = (position - target).length() if best is not None else math.inf
    try:
        nearest = geometry.nearestPrim(target)
    except hou.Error:
        nearest = None
    if nearest and nearest[0] is not None and nearest[3] < error * 0.5:
        prim, u, v, distance = nearest
        try:
            closest = prim.positionAt(u) if isinstance(prim, hou.Face) else prim.positionAt(u, v)
        except (hou.Error, AttributeError, TypeError):
            closest = target + hou.Vector3(distance, 0.0, 0.0)  # Only the distance is compared.
        return prim, closest
    if best is None and geometry.points():
        try:
            point = geometry.nearestPoint(target)
        except (hou.Error, AttributeError):
            point = None
        if point is not None:
            return None, point.position()
    return best


def _embedded(prim):
    """The embedded geometry of a packed primitive and its full transform, or None."""
    if not isinstance(prim, hou.PackedPrim):
        return None
    geometry = None
    if hasattr(prim, "getEmbeddedGeometry"):
        try:
            geometry = prim.getEmbeddedGeometry()
        except hou.Error:
            geometry = None
    if geometry is None:
        # Disk, Alembic and other packed types: unpack this one primitive.
        try:
            verb = hou.sopNodeTypeCategory().nodeVerb("unpack")
            source = hou.Geometry()
            source.merge(prim.geometry())
            keep = [other for other in source.prims() if other.number() != prim.number()]
            source.deletePrims(keep)
            verb.setParms({"limit_iterations": 1, "iterations": 1, "dotransform": 0})
            geometry = hou.Geometry()
            verb.execute(geometry, [source])
        except hou.Error:
            return None
    return geometry, prim.fullTransform()


def _hit_levels(geometry, origin, direction, target=None, depth=0):
    """(local hit position, [(geometry, prim), ...] innermost first), or None."""
    found = _intersect(geometry, origin, direction, target)
    if found is None:
        return None
    prim, position = found
    levels = [(geometry, prim)]
    if prim is not None and depth < MAX_PACKED_DEPTH:
        inner = _embedded(prim)
        if inner is not None:
            inner_geometry, transform = inner
            inverse = transform.inverted()
            local_origin = origin * inverse
            local_direction = (origin + direction) * inverse - local_origin
            nested = _hit_levels(
                inner_geometry,
                local_origin,
                local_direction.normalized(),
                None if target is None else target * inverse,
                depth + 1,
            )
            if nested is not None:
                position = nested[0] * transform
                levels = nested[1] + levels
    return position, levels


def _instance_geometry(obj, sop, geometry, now):
    """Packed prototype copies on the instancing points of obj: [(geometry, prototype object)]."""
    from ..scene import instancing

    attribute = geometry.findPointAttrib("instance")
    by_value = {}
    if attribute is not None and attribute.dataType() == hou.attribData.String:
        for point in geometry.points():
            by_value.setdefault(point.attribValue(attribute), []).append(point.number())
    if instancing.is_instancer(obj):
        fallback = obj.parm("instancepath").evalAtTime(now)
        if attribute is None:
            by_value = {fallback: [point.number() for point in geometry.points()]}
        elif "" in by_value:
            by_value[fallback] = by_value.pop("") + by_value.get(fallback, [])
    elif attribute is None:
        return []
    verb = hou.sopNodeTypeCategory().nodeVerb("copytopoints::2.0")
    verb.setParms({"pack": 1})
    result = []
    for value, numbers in by_value.items():
        if not value or instancing.is_blend_reference(value):
            continue
        prototype, _ = instancing.resolve(obj, value)
        if prototype is None or prototype.renderNode() is None:
            continue
        source = _geometry(prototype.renderNode(), now)
        if source is None:
            continue
        source = source.freeze()
        source.transform(prototype.worldTransform())
        points = hou.Geometry()
        points.merge(geometry)
        points.deletePrims(points.prims())
        wanted = set(numbers)
        points.deletePoints([point for point in points.points() if point.number() not in wanted])
        copies = hou.Geometry()
        verb.execute(copies, [source, points])
        result.append((copies, prototype))
    return result


def identify(rop, camera, position=None, ray=None, now=None):
    """The Hit owning a rendered pixel, or None.

    position: the renderer's world-space camera hit (Pick Focus planes); the object
    whose surface lies closest to it owns the pixel. ray: (origin, direction) when
    no renderer hit is available; the first surface along the camera ray owns it.
    """
    now = hou.time() if now is None else now
    if position is not None:
        target = _vector(position)
        origin = ray_origin(camera, target)
        direction = (target - origin).normalized()
    elif ray is not None:
        target = None
        origin, direction = ray
    else:
        return None
    from ..scene import instancing

    best = None
    for obj, sop in rendered_objects(rop, now):
        geometry = _geometry(sop, now)
        if geometry is None:
            continue
        transform = obj.worldTransformAtTime(now)
        inverse = transform.inverted()
        local_origin = origin * inverse
        local_direction = ((origin + direction) * inverse - local_origin).normalized()
        local_target = None if target is None else target * inverse
        candidates = [] if instancing.is_instancer(obj) else [(geometry, None)]
        try:
            candidates.extend(_instance_geometry(obj, sop, geometry, now))
        except hou.Error:
            pass
        for candidate, prototype in candidates:
            if not candidate.prims() and not candidate.points():
                continue
            found = _hit_levels(candidate, local_origin, local_direction, local_target)
            if found is None:
                continue
            hit = found[0] * transform
            metric = (hit - target).length() if target is not None else (hit - origin).length()
            if best is None or metric < best.error:
                best = Hit(obj, metric, found[1], prototype)
    if best is None:
        return None
    if target is not None and best.error > 0.25 * max((target - origin).length(), 1e-6):
        # The geometry the renderer saw is not what Houdini reports (an unknown procedural).
        return None
    return best


def _material_value(geometry, prim):
    if prim is not None:
        attribute = geometry.findPrimAttrib(MATERIAL_ATTRIBUTE)
        if attribute is not None and attribute.dataType() == hou.attribData.String:
            value = prim.attribValue(attribute)
            if value:
                return value
        points = geometry.findPointAttrib(MATERIAL_ATTRIBUTE)
        if points is not None and points.dataType() == hou.attribData.String and prim.vertices():
            value = prim.vertices()[0].point().attribValue(points)
            if value:
                return value
    detail = geometry.findGlobalAttrib(MATERIAL_ATTRIBUTE)
    if detail is not None and detail.dataType() == hou.attribData.String:
        return geometry.attribValue(detail)
    return ""


def material(rop, hit, now=None):
    """(material node or None, description) for a Hit, following the ROP's material policy."""
    now = hou.time() if now is None else now
    override = rop.parm("materialpath").evalAtTime(now) if rop.parm("materialpath") else ""
    if override:
        node = rop.node(override)
        return node, "ROP material override"
    if rop.parm("use_material_assignments") and not rop.parm("use_material_assignments").evalAtTime(now):
        return rop, "ROP fallback material (assignments are off)"
    owners = [hit.owner] if hit.prototype is None else [hit.prototype, hit.owner]
    for geometry, prim in hit.levels:
        value = _material_value(geometry, prim)
        if value:
            node = owners[0].node(value) or hou.node(value)
            if node is not None:
                return node, "geometry assignment"
    for obj in owners:
        parm = obj.parm(MATERIAL_ATTRIBUTE)
        value = parm.evalAtTime(now) if parm is not None else ""
        if value:
            node = obj.node(value)
            if node is not None:
                return node, "object assignment"
    return rop, "ROP fallback material"


def select(node):
    """Select node and make it current, as Houdini's viewport selection does."""
    node.setSelected(True, clear_all_selected=True)
    node.setCurrent(True)
    if hou.isUIAvailable():
        pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if pane is not None:
            pane.setCurrentNode(node)


def plane_value(frame, name, x, y):
    """Raw float values of plane name at full-raster pixel (x, y, bottom-up), or None."""
    plane = frame.get("planes", {}).get(name)
    if plane is None:
        return None
    x -= frame.get("x", 0)
    y -= frame.get("y", 0)
    width, height = frame["width"], frame["height"]
    if not (0 <= x < width and 0 <= y < height):
        return None
    channels = plane["channels"]
    return struct.unpack_from(f"{channels}f", plane["pixels"], (y * width + x) * channels * 4)


def display_value(image, x, y, full_size=None):
    """Displayed 8-bit RGBA at full-raster pixel (x, y, bottom-up) of a top-down QImage."""
    if image is None or image.isNull():
        return None
    width, height = full_size or image.size().toTuple()
    if not (0 <= x < width and 0 <= y < height):
        return None
    x = x * image.width() // width
    row = image.height() - 1 - y * image.height() // height
    color = image.pixelColor(x, row)
    return color.red(), color.green(), color.blue(), color.alpha()


def _number(value):
    if not math.isfinite(value):
        return str(value)
    return f"{value:.4f}" if abs(value) < 1e5 else f"{value:.4g}"


def readout_text(x, y, pass_name, values, color, display, channels=()):
    """One status-line part (no ' · ' separators: the headline keeps only its first parts)."""
    parts = [f"x {x}  y {y}"]
    label = pass_name or "Pass"
    if values is not None:
        names = list(channels) if len(channels) == len(values) else list("RGBA" if color else "XYZW")[: len(values)]
        if len(values) == 1 and not channels:
            names = [""]
        numbers = "  ".join((name + " " if name else "") + _number(value) for name, value in zip(names, values))
        parts.append(label + (" linear " if color else " ") + numbers)
    elif display is not None:
        parts.append(label + " (no linear values)")
    if display is not None:
        parts.append("display " + " ".join(str(v) for v in display))
    return "    ".join(parts)
