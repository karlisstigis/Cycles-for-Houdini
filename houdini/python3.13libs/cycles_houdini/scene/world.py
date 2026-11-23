# SPDX-License-Identifier: Apache-2.0
"""Owned world definitions for native SOP, IPR and batch rendering.

Native environment lights retain Houdini's Y-up convention. Authored World
Material graphs use Cycles' direction conventions and explicit Mapping nodes.
"""

import math
import os

import hou

from . import object_scene


def is_environment(node):
    return node.type().category() == hou.objNodeTypeCategory() and node.type().name().split("::")[0] == "envlight"


def _node(name, type, **parameters):
    return dict(id=name, definition="cycles/" + type + "/1", parameters=parameters)


def _link(source, output, destination, input):
    return {"from": dict(node=source, socket=output), "to": dict(node=destination, socket=input)}


def native_environment(node, now, diagnostics=None):
    value = lambda name, default: object_scene.value(node, name, now, default)

    def ignored(parameter, reason):
        if diagnostics is not None:
            diagnostics.append(
                dict(
                    code="world.unsupported_setting",
                    source=node.path(),
                    resource="world",
                    message=parameter + ": " + reason,
                    action="Ignoring the unsupported setting; retaining supported environment lighting",
                )
            )

    if value("skymap_enable", False):
        raise ValueError(
            "Houdini procedural sky requires a sky adapter: "
            + node.path()
            + ". Use an environment image or Cycles World Material."
        )
    intensity, exposure = value("light_intensity", 1), value("light_exposure", 0)
    color = node.parmTuple("light_color").evalAtTime(now)
    if (
        not math.isfinite(intensity)
        or intensity < 0
        or not math.isfinite(exposure)
        or any(not math.isfinite(c) or c < 0 for c in color)
    ):
        raise ValueError("Environment power and color must be finite and nonnegative: " + node.path())
    try:
        strength = intensity * 2**exposure
    except OverflowError:
        raise ValueError("Environment exposure exceeds finite radiometric storage: " + node.path()) from None
    float_max = 3.402823466e38
    if (
        not math.isfinite(strength)
        or strength > float_max
        or any(c > float_max or c * strength > float_max for c in color)
    ):
        raise ValueError(
            "Environment power and color must fit nonnegative finite float32 radiometric storage: " + node.path()
        )
    nodes = [_node("background", "background_shader", color=tuple(color), strength=float(strength))]
    links = []
    path = value("env_map", "")
    if path:
        if not path.startswith("op:"):
            path = os.path.normpath(path if os.path.isabs(path) else os.path.join(hou.expandString("$HIP"), path))
        matrix = hou.Matrix3(node.worldTransformAtTime(now))
        axes = [hou.Vector3(axis) * matrix for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
        if min(axis.length() for axis in axes) <= 1e-9:
            raise ValueError("Environment orientation has a zero-scale axis: " + node.path())
        axes = [axis.normalized() for axis in axes]
        if max(abs(axes[a].dot(axes[b])) for a, b in ((0, 1), (0, 2), (1, 2))) > 1e-5:
            raise ValueError(
                "Sheared environment orientation requires an explicit World Material mapping: " + node.path()
            )
        # Dot with the world-space local axes: inverse rotation, no translation.
        # Match the native environment light's longitude origin as well as its
        # Y-up pole: local -Z is the image centre. Cycles uses +X there, so the
        # conversion is (-z, -x, y), including the native quarter-turn in yaw.
        nodes += [
            _node("direction", "geometry"),
            _node("coordinates", "combine_xyz"),
            _node(
                "image",
                "environment_texture",
                filename=path,
                colorspace=value("cycles_environment_colorspace", "auto"),
                alpha_type="ignore",
            ),
            _node("tint", "vector_math", math_type="multiply", vector2=tuple(color)),
        ]
        for name, axis in zip("xyz", (-axes[2], -axes[0], axes[1])):
            nodes.append(_node("axis_" + name, "vector_math", math_type="dot_product", vector2=tuple(axis)))
            links += [
                _link("direction", "position", "axis_" + name, "vector1"),
                _link("axis_" + name, "value", "coordinates", name),
            ]
        links += [
            _link("coordinates", "vector", "image", "vector"),
            _link("image", "color", "tint", "vector1"),
            _link("tint", "vector", "background", "color"),
        ]
    if value("env_clipy", False):
        # The native hemisphere is the environment light's local +Y pole.
        pole = hou.Vector3(0, 1, 0) * hou.Matrix3(node.worldTransformAtTime(now))
        if pole.length() <= 1e-9:
            raise ValueError("Environment hemisphere has a zero-scale axis: " + node.path())
        if not path:
            nodes.append(_node("direction", "geometry"))
        nodes += [
            _node("hemisphere", "vector_math", math_type="dot_product", vector2=tuple(pole.normalized())),
            _node("positive", "math", math_type="greater_than", value2=0.0),
            _node("masked_strength", "math", math_type="multiply", value2=float(strength)),
        ]
        links += [
            _link("direction", "position", "hemisphere", "vector1"),
            _link("hemisphere", "value", "positive", "value1"),
            _link("positive", "value", "masked_strength", "value1"),
            _link("masked_strength", "value", "background", "strength"),
        ]
    visibility = 255
    for name, masks in (("light_contribprimary", 1), ("light_contribdiff", 4), ("light_contribspec", 2 | 8)):
        if not value(name, name != "light_contribprimary"):
            visibility &= ~masks
    if value("cycles_light_visibility_override", False):
        visibility = 255
        for name, mask in (("camera", 1), ("transmission", 2), ("diffuse", 4), ("glossy", 8), ("volume", 16)):
            if not value("cycles_light_visibility_" + name, True):
                visibility &= ~mask
    shadow = object_scene.token(node, "shadow_type", now, "raytrace")
    cast_shadow = shadow != "off" and bool(value("cycles_light_cast_shadow", True))
    # Native disabled controls are dormant, not unsupported requests. In
    # particular, occlusion distance/angle/adaptive options do not participate
    # in Direct Lighting. The mode diagnostic covers them when AO is selected.
    mode = value("env_mode", "direct")
    if mode != "direct":
        ignored(
            "env_mode",
            "Native " + str(mode) + " mode and its sampling controls are not supported; using Direct Lighting",
        )
    else:
        if value("env_portalenable", False):
            ignored("env_portalenable", "Native environment portals require an additional adapter")
        if path and value("env_filteramount", 0) != 0:
            ignored(
                "env_filteramount", "Native BSDF-dependent glossy environment filtering requires an additional adapter"
            )
    if value("light_contrib", 0):
        ignored(
            "light_contrib",
            "Native contribution lists require an additional adapter; retaining supported ray-visibility controls",
        )
    if cast_shadow:
        if value("shadowmask", "*") != "*" or value("shadowcategories", ""):
            ignored(
                "shadowmask",
                "The pinned Cycles background path does not implement world shadow linking; retaining ordinary occlusion",
            )
        if shadow not in ("off", "raytrace"):
            ignored("shadow_type", "Unsupported native shadow type; using ray-traced shadows")
        if value("shadow_intensity", 1) != 1:
            ignored("shadow_intensity", "Native partial-shadow intensity requires an additional adapter")
        if not value("shadow_transparent", True):
            ignored("shadow_transparent", "Native opaque-shadow override requires an additional adapter")
    return dict(
        world=dict(
            source_path=node.path(),
            graph=dict(
                schema_version=1, nodes=nodes, links=links, surface=dict(node="background", socket="background")
            ),
            visibility=visibility,
            use_mis=bool(value("cycles_light_use_mis", True)),
            cast_shadow=cast_shadow,
            use_caustics=bool(value("cycles_light_use_caustics", False)),
            max_bounces=int(value("cycles_light_max_bounces", 1024)),
            map_resolution=int(value("cycles_environment_map_resolution", 0)),
        ),
        background_lightgroup=value("cycles_lightgroup", ""),
        background_shadow_catcher=bool(value("cycles_light_shadow_catcher", True)),
    )


def _shader_state(graph):
    # Owned image context selects pixels; it is not an animated engine socket.
    return {name: value for name, value in graph.items() if name != "image_time"}


def _configuration(rop, now, times=(), diagnostics=None):
    from ..materials import material_graph, WORLD_BUILDER

    value = lambda name, default: object_scene.value(rop, name, now, default)

    def motion_ignored(source):
        if diagnostics is not None:
            diagnostics.append(
                dict(
                    code="world.motion_ignored",
                    source=source,
                    resource="world",
                    message="World lighting varies over the shutter; temporal world sampling is not supported",
                    action="Rendering world lighting at the requested frame without world motion blur",
                )
            )

    mode = value("world_source", "scene")
    sampling = value("world_sampling", "auto")
    if mode not in ("scene", "color", "material") or sampling not in ("auto", "manual", "none"):
        raise ValueError("Unsupported Cycles world source or sampling mode")
    settings = dict(
        graph=None,
        source_path=rop.path(),
        use_mis=sampling != "none",
        map_resolution=int(value("world_map_resolution", 1024)) if sampling == "manual" else 0,
        max_bounces=int(value("world_max_bounces", 1024)),
        volume_sampling=value("world_volume_sampling", "equiangular"),
        cast_shadow=bool(value("world_cast_shadow", True)),
        use_caustics=bool(value("world_use_caustics", False)),
    )
    settings["visibility"] = 255
    for name, mask in (("camera", 1), ("transmission", 2), ("diffuse", 4), ("glossy", 8), ("volume", 16)):
        if not value("world_visibility_" + name, True):
            settings["visibility"] &= ~mask
    if mode == "material":
        path = value("world_material", "")
        builder = rop.node(path[3:] if path.startswith("op:") else path) if path else None
        if builder is None or builder.type().name() != WORLD_BUILDER:
            raise ValueError("Choose a Cycles World Material on the ROP: " + str(path))
        settings["graph"] = material_graph(builder, now)
        settings["source_path"] = builder.path()
        animated = False
        for sample in times:
            if sample != now:
                try:
                    animated |= _shader_state(material_graph(builder, sample)) != _shader_state(settings["graph"])
                except ValueError:
                    animated = True  # An unusable shutter-only state must not discard a usable nominal world.
        if animated:
            motion_ignored(builder.path())
    elif mode == "scene":
        lights = [light for light in object_scene.selected(rop, now, lights=True) if is_environment(light)]
        animated = False
        for sample in times:
            if sample != now and [
                light.sessionId() for light in object_scene.selected(rop, sample, lights=True) if is_environment(light)
            ] != [light.sessionId() for light in lights]:
                animated = True
        if len(lights) > 1:
            raise ValueError(
                "Cycles has one world: select one Environment Light with the ROP light patterns, or combine environments in a Cycles World Material. Selected: "
                + ", ".join(light.path() for light in lights)
            )
        if lights:
            result = native_environment(lights[0], now, diagnostics)
            for sample in times:
                if sample != now:
                    try:
                        animated |= native_environment(lights[0], sample) != result
                    except ValueError:
                        animated = True
            if animated:
                motion_ignored(lights[0].path())
            return result
        if animated:
            motion_ignored(rop.path())
        # Scene-light mode uses the native light's settings. With no environment
        # selected, use the configured constant world and default engine policy.
        settings = dict(graph=None)
    return dict(
        world=settings,
        background_lightgroup=value("background_lightgroup", ""),
        background_shadow_catcher=bool(value("background_shadow_catcher", True)),
    )


def configuration(rop, now, times=(), diagnostics=None):
    try:
        return _configuration(rop, now, times, diagnostics)
    except ValueError as error:
        if diagnostics is not None:
            diagnostics.append(
                dict(
                    code="world.unavailable",
                    source=rop.path(),
                    resource="world",
                    message=str(error),
                    action="Using the configured constant background",
                )
            )
        return dict(
            world=dict(graph=None, source_path=rop.path()), background_lightgroup="", background_shadow_catcher=True
        )
