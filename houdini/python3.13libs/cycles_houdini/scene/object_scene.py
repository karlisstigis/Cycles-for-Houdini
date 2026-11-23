# SPDX-License-Identifier: Apache-2.0
"""Public OBJ selection and numeric-light semantics for native render consumers.

This module never cooks or copies geometry. HDK owns that work and its resources.
"""

import math
import re
import hou
from .. import matrix_from_houdini
from . import light_properties


def value(node, name, now, default):
    parm = node.parm(name)
    return parm.evalAtTime(now) if parm else default


def token(node, name, now, default):
    parm = node.parm(name)
    return parm.evalAsStringAtFrame(hou.timeToFrame(now)) if parm else default


def enabled(rop, now):
    mode = value(rop, "source_mode", now, "sop")
    if mode not in ("sop", "objects"):
        raise ValueError("Unsupported native source mode: " + str(mode))
    return mode == "objects"


def root(rop, now):
    path = value(rop, "objectroot", now, "/obj")
    node = rop.node(path)
    if node is None or node.childTypeCategory() != hou.objNodeTypeCategory():
        raise ValueError("Scene root must contain OBJ nodes: " + path)
    return node


def candidates(rop, now):
    from .instancing import is_generated

    return tuple(
        node for node in root(rop, now).recursiveGlob("*", filter=hou.nodeTypeFilter.Obj) if not is_generated(node)
    )


def selected(rop, now, lights=False):
    network = root(rop, now)
    category = hou.nodeTypeFilter.ObjLight if lights else hou.nodeTypeFilter.ObjGeometry
    names = ("lights", "forcelights", "excludelights") if lights else ("vobject", "forceobject", "excludeobject")

    def match(mask):
        return set(network.recursiveGlob(mask, filter=category))

    normal = match(value(rop, names[0], now, "*"))
    forced = match(value(rop, names[1], now, ""))
    excluded = match(value(rop, names[2], now, ""))
    result = []
    from .instancing import is_generated

    for node in sorted((normal | forced) - excluded, key=lambda node: node.sessionId()):
        if is_generated(node):
            continue
        if node.type().category() != hou.objNodeTypeCategory():
            continue
        if lights:
            if not value(node, "light_enable", now, True):
                continue
        else:
            # The native Null contains a render-flagged control SOP, but that
            # geometry is a viewport glyph. A Null remains a transform source,
            # including when parented to lights, and never emits geometry.
            if node.type().name().split("::")[0] == "null":
                continue
            sop = node.renderNode()
            # ObjGeometry also matches object subnets. Their OBJ output is not
            # renderable SOP geometry; their actual child objects are selected.
            if sop is None or sop.type().category() != hou.sopNodeTypeCategory():
                continue
            if node not in forced and not node.isObjectDisplayedAtFrame(hou.timeToFrame(now)):
                continue
        result.append(node)
    return result


def selection_signature(rop, now):
    """Cheap membership check for bundle edits, which emit no node callback."""
    if not enabled(rop, now):
        return None
    return tuple(tuple(node.sessionId() for node in selected(rop, now, lights)) for lights in (False, True))


# Public integration contract, matched to the pinned Cycles visibility flags.
_all_visibility = 255
_ray_masks = {
    "*": _all_visibility,
    "primary": 1,
    "refract": 2,
    "diffuse": 4,
    "reflect": 8,
    "volume": 16,
    "shadow": 96,
    "raycast": 128,
}


def visibility(expression):
    """Parse Houdini's ray-set union/intersection/complement without eval()."""
    if not expression.strip():
        return 0
    parts = re.findall(r"[A-Za-z_]+|[\*\|&()\-]", expression)
    if "".join(parts) != re.sub(r"\s+", "", expression):
        raise ValueError("Unsupported native render visibility expression: " + expression)
    position = 0

    def atom():
        nonlocal position
        if position >= len(parts):
            raise ValueError("Incomplete native render visibility expression: " + expression)
        part = parts[position]
        position += 1
        if part == "-":
            return _all_visibility ^ atom()
        if part == "(":
            result = union()
            if position >= len(parts) or parts[position] != ")":
                raise ValueError("Unclosed native render visibility expression: " + expression)
            position += 1
            return result
        if part not in _ray_masks:
            raise ValueError("Unsupported native render visibility ray type: " + part)
        return _ray_masks[part]

    def intersection():
        nonlocal position
        result = atom()
        while position < len(parts) and parts[position] == "&":
            position += 1
            result &= atom()
        return result

    def union():
        nonlocal position
        result = intersection()
        while position < len(parts) and parts[position] == "|":
            position += 1
            result |= intersection()
        return result

    result = union()
    if position != len(parts):
        raise ValueError("Invalid native render visibility expression: " + expression)
    return result


def object_visibility(node, now):
    result = visibility(value(node, "vm_rendervisibility", now, "*"))
    if value(node, "vm_phantom", now, False):
        result &= ~1
    if value(node, "cycles_visibility_override", now, False):
        for name, mask in (
            ("camera", 1),
            ("transmission", 2),
            ("diffuse", 4),
            ("glossy", 8),
            ("volume", 16),
            ("shadow", 96),
            ("raycast", 128),
        ):
            if not value(node, "cycles_visibility_" + name, now, True):
                result &= ~mask
    return result


def _light_definition(node, now, diagnostics=None):
    def ignored(message):
        if diagnostics is not None:
            diagnostics.append(
                dict(
                    code="light.unsupported_setting",
                    source=node.path(),
                    resource=f"light:{node.sessionId()}",
                    message=message,
                    action="Ignoring the unsupported setting; rendering the supported light",
                )
            )

    kind = light_properties.light_type(node, now)
    cycles_light = light_properties.is_cycles_light(node)
    if kind not in (
        ("point", "spot", "grid", "sun") if cycles_light else ("point", "grid", "disk", "sphere", "distant", "sun")
    ):
        raise ValueError(
            "Cycles has no analytic "
            + (kind or node.type().name())
            + " light. "
            + node.path()
            + ": use a Cycles Point, Spot, Rectangle, Disk or Sun light; mesh-emitter adaptation for this Houdini shape is not implemented."
        )
    for name in ("areamap", "light_texture", "projmap"):
        if name == "areamap" and kind in ("grid", "disk"):
            continue
        if value(node, name, now, ""):
            ignored("Native textured/projected lights need an additional adapter: " + node.path())
    cone = None
    if not cycles_light and kind not in ("sun", "distant") and value(node, "coneenable", now, False):
        angle = value(node, "coneangle", now, 45)
        delta = value(node, "conedelta", now, 10)
        roll = value(node, "coneroll", now, 1)
        if not all(math.isfinite(x) for x in (angle, delta, roll)) or not 0 <= angle <= 360 or delta < 0 or roll <= 0:
            raise ValueError(
                "Cone Angle must be 0–360°, Cone Delta nonnegative, and Cone Rolloff positive: " + node.path()
            )
        cone = dict(
            inner_half_angle=math.radians(angle * 0.5),
            outer_half_angle=math.radians(min(angle * 0.5 + delta, 180)),
            rolloff=roll,
        )
        if value(node, "sharpspot", now, False):
            ignored(
                "Sharp Spot Light requires origin-based projection, which this Cycles adapter does not implement: "
                + node.path()
            )
    if kind in ("grid", "disk", "sphere") and value(node, "edgeenable", now, False):
        ignored(
            "Native area edge falloff is not implemented; use a supported Cycles light or an emissive material: "
            + node.path()
        )
    if kind not in ("sun", "distant") and (
        token(node, "atten_type", now, "physical") != "physical"
        or value(node, "atten_start", now, 0) != 0
        or value(node, "attenrampenable", now, False)
    ):
        ignored("Native numeric lights require physical attenuation without a ramp: " + node.path())
    shadow = "raytrace" if cycles_light else token(node, "shadow_type", now, "raytrace")
    if shadow not in ("off", "raytrace"):
        ignored("Native depth-map light shadows require an additional adapter: " + node.path())
    if value(node, "light_contrib", now, 0):
        ignored("Native light contribution overrides require an additional adapter: " + node.path())
    if value(node, "shadow_intensity", now, 1) != 1:
        ignored("Native colored/partial shadow controls require an additional adapter: " + node.path())
    shadow_color = node.parmTuple("shadow_color")
    if shadow_color and any(shadow_color.evalAtTime(now)):
        ignored("Native colored/partial shadow controls require an additional adapter: " + node.path())
    color = light_properties.emission_color(node, now)
    intensity, exposure = value(node, "light_intensity", now, 1), value(node, "light_exposure", now, 0)
    if not math.isfinite(intensity) or not math.isfinite(exposure) or intensity < 0:
        raise ValueError(
            "Native light intensity and exposure must be finite; intensity must be nonnegative: " + node.path()
        )
    try:
        strength = tuple(channel * intensity * 2**exposure for channel in color)
    except OverflowError:
        raise ValueError("Native light exposure exceeds finite radiometric storage: " + node.path()) from None
    if not all(math.isfinite(channel) and 0 <= channel <= 3.402823466e38 for channel in strength):
        raise ValueError("Native light power must fit nonnegative finite float32 RGB: " + node.path())
    result = dict(
        key=f"light:{node.sessionId()}",
        kind=kind
        if kind in ("point", "spot")
        else "area"
        if kind in ("grid", "disk")
        else "point"
        if kind == "sphere"
        else "sun",
        transform=matrix_from_houdini(node.worldTransformAtTime(now)),
        strength=strength,
        radius=0.0,
        normalize=(not cycles_light and kind in ("point", "spot")) or bool(value(node, "normalizearea", now, True)),
        cast_shadow=shadow != "off",
        use_mis=True,
        visibility=255 if value(node, "light_contribprimary", now, False) else 254,
    )
    result.update(
        lightgroup=value(node, "cycles_lightgroup", now, ""),
        radius=value(node, "cycles_light_radius", now, 0),
        cryptomatte_object=value(node, "cycles_cryptomatte_object", now, "") or node.path(),
        cryptomatte_asset=value(node, "cycles_cryptomatte_asset", now, "") or node.path(),
        is_sphere=bool(value(node, "cycles_light_is_sphere", now, True)),
        spread=math.radians(value(node, "cycles_light_spread", now, 180)),
        spot_smooth=value(node, "cycles_light_spot_smooth", now, 0),
        max_bounces=value(node, "cycles_light_max_bounces", now, 1024),
        use_caustics=bool(value(node, "cycles_light_use_caustics", now, False)),
        use_mis=bool(value(node, "cycles_light_use_mis", now, True)),
        cast_shadow=result["cast_shadow"] and bool(value(node, "cycles_light_cast_shadow", now, True)),
        shadow_catcher=bool(value(node, "cycles_light_shadow_catcher", now, True)),
    )
    if cycles_light or value(node, "cycles_light_visibility_override", now, False):
        result["visibility"] = 255
        for name, mask in (("camera", 1), ("transmission", 2), ("diffuse", 4), ("glossy", 8), ("volume", 16)):
            if not value(node, "cycles_light_visibility_" + name, now, name != "camera"):
                result["visibility"] &= ~mask
    if kind in ("grid", "disk"):
        axes = [[result["transform"][row * 4 + column] for row in range(3)] for column in range(3)]
        lengths = [math.sqrt(sum(value * value for value in axis)) for axis in axes]
        if any(not math.isfinite(length) or length <= 0 for length in lengths):
            raise ValueError("Native rectangular lights require finite nonzero axes: " + node.path())
        if any(
            abs(sum(axes[a][row] * axes[b][row] for row in range(3))) > 1e-5 * lengths[a] * lengths[b]
            for a, b in ((0, 1), (0, 2), (1, 2))
        ):
            raise ValueError("Sheared native rectangular lights need a geometry-light adapter: " + node.path())
        result["size"] = light_properties.area_size(node, now)
        result["ellipse"] = kind == "disk"
        if cycles_light and kind == "grid":
            shape = value(node, "cycles_light_area_shape", now, "square")
            if shape not in ("square", "rectangle", "disk", "ellipse"):
                raise ValueError("Unsupported Cycles area shape: " + str(shape) + " on " + node.path())
            result["ellipse"] = shape in ("disk", "ellipse")
            if shape in ("square", "disk"):
                result["size"] = (result["size"][0], result["size"][0])
        result["two_sided"] = not bool(value(node, "singlesided", now, False))
        result["reverse"] = bool(value(node, "reverse", now, False))
        if value(node, "cycles_light_is_portal", now, False):
            # One portal facing the light direction; it emits nothing, so no back side or emission graph.
            result.update(is_portal=True, two_sided=False)
    elif kind in ("distant", "sun"):
        result["angle"] = math.radians(value(node, "vm_envangle", now, 5)) if kind == "sun" else 0.0
    elif kind == "spot":
        result["angle"] = math.radians(value(node, "cycles_light_spot_angle", now, 45))
    elif kind == "sphere":
        size = node.parmTuple("areasize").evalAtTime(now)
        if not all(math.isfinite(x) and x > 0 for x in size) or not math.isclose(size[0], size[1], rel_tol=1e-6):
            raise ValueError(
                "Cycles spherical lights require equal positive Area Size dimensions; ellipsoidal mesh-emitter adaptation is not implemented: "
                + node.path()
            )
        result.update(radius=size[0] * 0.5, is_sphere=True)
    if cone:
        result["cone"] = cone
        if result["kind"] == "point" and 0 < cone["outer_half_angle"] <= math.pi * 0.5:
            result.update(kind="spot", angle=cone["outer_half_angle"] * 2, spot_smooth=0)
    from . import light_shaders

    shader = None if result.get("is_portal") else light_shaders.graph(node, now, kind, result.get("size"), diagnostics)
    if shader:
        result["graph"] = shader
    result["source_path"] = node.path()
    result["linking_id"] = node.path()
    return result


def light_definitions(node, now, motion_times, diagnostics=None):
    """Owned definitions at the requested frame.

    Analytic lights do not move over the shutter (as in Cycles in Blender); an
    emissive mesh does. ``motion_times`` is accepted for existing callers and is
    not evaluated.
    """
    try:
        result = _light_definition(node, now, diagnostics)
    except ValueError as error:
        if diagnostics is not None:
            diagnostics.append(
                dict(
                    code="light.unavailable",
                    source=node.path(),
                    resource=f"light:{node.sessionId()}",
                    message=str(error),
                    action="Omitting this light; rendering the rest of the scene",
                )
            )
        return []
    two_sided, reverse = result.pop("two_sided", False), result.pop("reverse", False)
    if reverse and not two_sided:
        result["transform"] = [
            value * (-1 if index % 4 in (0, 2) else 1) for index, value in enumerate(result["transform"])
        ]
        if "cone" in result:
            result["cone"] = dict(result["cone"], reverse_axis=True)
    if not two_sided:
        return [result]
    # The pinned engine uses one-sided analytic rectangles. Split normalized
    # total radiant power over two owned lights; unnormalized radiance stays
    # identical on each side. A half-turn gives the back a proper transform.
    result["key"] += "/front"
    if result["normalize"]:
        result["strength"] = tuple(channel * 0.5 for channel in result["strength"])
    back = dict(
        result,
        key=f"light:{node.sessionId()}/back",
        transform=[value * (-1 if index % 4 in (0, 2) else 1) for index, value in enumerate(result["transform"])],
    )
    if "cone" in result:
        back["cone"] = dict(result["cone"], reverse_axis=True)
    return [result, back]
