# SPDX-License-Identifier: Apache-2.0
"""Host-side orchestration; geometry and camera extraction remain in the HDK adapter."""

import hou
import os
import re
import posixpath
import math
from .. import geometry_request, matrix_from_houdini
from . import object_scene, world
from ..core import device_selection
from ..rop.rop_controls import value as rop_value


def _stylesheet_nodes(resources):
    return tuple(
        sorted(
            {
                resource[4:] if resource.startswith("vex:op:") else resource[3:]
                for resource in resources
                if resource.startswith(("vex:op:", "op:"))
            }
        )
    )


def object_for_sop(sop):
    node = sop.parent()
    while node and node.type().category() != hou.objNodeTypeCategory():
        node = node.parent()
    if node is None:
        raise RuntimeError("Geometry must be a SOP inside an OBJ")
    return node


def configuration(rop, at_time=None, *, interactive=True):
    if rop is None or rop.type().name() != "cycles_ipr":
        raise ValueError("Select a Cycles Native IPR ROP")
    now = hou.time() if at_time is None else at_time
    from ..rop.output import dimensions

    values = (
        device_selection.selected_devices(rop, now),
        *dimensions(rop, now, interactive=interactive),
        *(rop.parm(name).evalAtTime(now) for name in ("samples", "use_texture_cache", "auto_texture_cache")),
    )
    samples = rendering_configuration(rop, now, interactive=interactive)["samples"]
    return (
        *values[:3],
        samples,
        *values[4:],
        int(rop_value(rop, "render_threads", now)),
        bool(not interactive and rop_value(rop, "use_tiling", now)),
        int(rop_value(rop, "tile_size", now)) if not interactive else 2048,
        # Curve Subdivisions is a Cycles SceneParams value: a change recreates the session.
        curve_subdivisions(rop, now),
        # Mesh refit applies to live renders only; final and batch renders always rebuild.
        refit_meshes(rop, now) if interactive else False,
    )


def curve_subdivisions(rop, now):
    """The ROP's Curve Subdivisions; a ROP saved before the parameter existed renders with 2,
    Blender's default and the value that was fixed before."""
    parm = rop.parm("curve_subdivisions")
    return 2 if parm is None else int(parm.evalAtTime(now))


def refit_meshes(rop, now):
    """The ROP's Refit Meshes While Interacting; a ROP saved before the parameter existed rebuilds."""
    parm = rop.parm("ipr_refit_meshes")
    return False if parm is None else bool(parm.evalAtTime(now))


def session_identity(configuration):
    """Samples update live; device/dimensions/cache/threads/curve subdivisions recreate the
    session."""
    return (*configuration[:3], *configuration[4:])


def interactive_settings(rop, interactive=True):
    return bool(interactive and rop_value(rop, "viewer_interactive", hou.time()))


def set_interactive_denoising(rop, enabled):
    """Viewer and ROP share one interactive switch; final settings stay intact."""
    with hou.undos.group("Interactive Denoising"):
        rop.parm("ipr_use_denoise" if interactive_settings(rop) else "use_denoise").set(bool(enabled))


def denoising_configuration(rop, at_time, *, interactive=True):
    override = interactive_settings(rop, interactive)

    def value(name):
        return rop_value(rop, "ipr_" + name if override else name, at_time)

    enabled = bool(value("use_denoise"))
    result = dict(
        enabled=enabled,
        method=value("denoiser"),
        use_gpu=bool(value("denoise_use_gpu")),
        quality=value("denoise_quality"),
        prefilter=value("denoise_prefilter"),
        start_sample=int(value("denoise_start_sample")),
        passes=value("denoise_passes"),
    )
    if result["method"] == "dlss":
        if not override:
            raise ValueError("DLSS is available only in Interactive Render settings")
        quality = rop_value(rop, "ipr_dlss_quality", at_time)
        choices = {
            "native": ("high", 1.0),
            "quality": ("high", 1.5),
            "balanced": ("balanced", 1.0 / 0.58),
            "performance": ("fast", 2.0),
            "ultra_performance": ("fast", 3.0),
        }
        if quality not in choices:
            raise ValueError("Unknown DLSS quality: " + str(quality))
        result["quality"], result["upscale_factor"] = choices[quality]
        result["start_sample"] = 0
    return result


def rendering_configuration(rop, at_time, *, interactive=True):
    """Authored host policy, including separate IPR budgets and native time evaluation."""
    parameters = (
        "samples",
        "adaptive_sampling",
        "noise_threshold",
        "min_samples",
        "time_limit",
        "sampling_pattern",
        "seed",
        "animate_seed",
        "min_bounce",
        "max_bounce",
        "diffuse_bounce",
        "glossy_bounce",
        "transmission_bounce",
        "volume_bounce",
        "transparent_min_bounce",
        "transparent_max_bounce",
        "light_tree",
        "light_threshold",
        "clamp_direct",
        "clamp_indirect",
        "filter_glossy",
        "resampled_light_sampling",
        "use_guiding",
        "deterministic_guiding",
        "nested_dielectrics",
        "caustics_reflective",
        "caustics_refractive",
        "fast_gi",
        "fast_gi_method",
        "ao_bounces",
        "ao_factor",
        "ao_distance",
        "volume_biased",
        "volume_step_rate",
        "volume_max_steps",
        "film_exposure",
        "filter_width",
        "pass_alpha_threshold",
        "pixel_filter",
        "mist_start",
        "mist_depth",
        "mist_falloff",
        "sample_subset",
        "subset_offset",
        "subset_length",
    )
    toggles = {
        "adaptive_sampling",
        "animate_seed",
        "light_tree",
        "caustics_reflective",
        "caustics_refractive",
        "fast_gi",
        "volume_biased",
        "sample_subset",
        "resampled_light_sampling",
        "use_guiding",
        "deterministic_guiding",
        "nested_dielectrics",
    }
    override = interactive_settings(rop, interactive)
    result = {}
    for name in parameters:
        selected = (
            "ipr_" + name
            if override and name in ("samples", "adaptive_sampling", "noise_threshold", "min_samples")
            else name
        )
        if override and name in ("volume_step_rate", "ao_bounces"):
            selected = "ipr_" + name
        value = rop_value(rop, selected, at_time)
        result[name] = bool(value) if name in toggles else value
    if interactive:
        result["time_limit"] = 0.0
        result["sample_subset"] = False
    # Changing time with a fixed seed must not reset otherwise identical renders.
    result["frame"] = hou.timeToFrame(at_time) if result["animate_seed"] else 1.0
    result["mist_falloff"] = float(result["mist_falloff"])
    return result


def material_definition(rop, at_time=None, shutter_times=None):
    from ..materials import MaterialSourceError

    try:
        return _material_definition(rop, at_time, shutter_times)
    except MaterialSourceError as error:
        now = hou.time() if at_time is None else at_time
        path = rop.parm("materialpath").evalAtTime(now)
        key = f"rop:{rop.sessionId()}/surface"
        return dict(
            key=key,
            kind="diagnostic_surface",
            color=(1, 0, 1),
            cryptomatte_name=path,
            source_diagnostics=[
                dict(
                    code="material.invalid_override",
                    source=path,
                    resource=key,
                    message=str(error),
                    action="Using a magenta diagnostic material for the override",
                )
            ],
        )


def _graph_material(builder, key, now, shutter_times=None, evaluation=None):
    from ..materials import material_graph, MaterialSourceError

    def capture(time):
        return (
            evaluation.graph(builder, time)
            if hasattr(evaluation, "graph")
            else material_graph(builder, time, evaluation=evaluation)
        )

    graph = capture(now)
    result = dict(
        key=key,
        kind="graph",
        graph=graph,
        cryptomatte_name=builder.path(),
        pass_index=(
            evaluation.parm(builder.parm("cycles_pass_index"), now)
            if evaluation
            else builder.parm("cycles_pass_index").evalAtTime(now)
        )
        if builder.parm("cycles_pass_index")
        else 0,
    )
    if (
        graph.get("displacement_time_dependent")
        and graph.get("displacement_method") in ("true", "both")
        and shutter_times
        and len(shutter_times) >= 3
    ):
        samples = []
        try:
            for time in shutter_times:
                sample = capture(time)
                if not sample.get("displacement") or sample.get("displacement_method") != graph["displacement_method"]:
                    raise MaterialSourceError("Displacement output/method changes during the shutter")
                sample["displacement_time_dependent"] = False  # All values are now owned constants.
                samples.append(sample)
        except MaterialSourceError as error:
            result["source_diagnostics"] = [
                dict(
                    code="motion.displacement_source",
                    source=builder.path(),
                    resource=key,
                    message=str(error),
                    action="Using requested-frame displacement without incompatible motion blur",
                )
            ]
        else:
            result["displacement_samples"] = samples
    return result


def _material_definition(rop, at_time=None, shutter_times=None):
    now = hou.time() if at_time is None else at_time
    path = rop.parm("materialpath").evalAtTime(now)
    if path:
        from ..materials import material_graph, MaterialSourceError

        builder = rop.node(path)
        if builder is None:
            raise MaterialSourceError("Missing Cycles material: " + path)
        return _graph_material(builder, f"rop:{rop.sessionId()}/surface", now, shutter_times)
    result = dict(
        key=f"rop:{rop.sessionId()}/surface",
        kind=rop.parm("material_kind").evalAtTime(now),
        cryptomatte_name=rop.path() + "/FallbackSurface",
        pass_index=rop_value(rop, "material_pass_index", now),
        color=rop.parmTuple("basecolor").evalAtTime(now),
        color_attribute=rop.parm("color_attribute").evalAtTime(now),
        volume_emission_color=rop.parmTuple("volume_emission_color").evalAtTime(now),
        emission_field=rop.parm("emission_field").evalAtTime(now),
        strength=rop.parm("emission_strength").evalAtTime(now),
    )
    for name in (
        "roughness",
        "metallic",
        "density",
        "anisotropy",
        "volume_emission_strength",
        "blackbody_intensity",
        "temperature_kelvin_scale",
    ):
        result[name] = rop.parm(name).evalAtTime(now)
    return result


def resolve_materials(references, at_time, *, shutter_times=None):
    """Host acquisition: evaluate owned graphs before geometry conversion."""
    import threading

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Material graph preparation requires the host thread")
    from ..materials import material_graph, MaterialSourceError

    result = []
    for reference in references:
        builder = hou.node(reference["path"])
        if builder is None:
            raise MaterialSourceError("Missing Cycles material: " + reference["path"])
        if (
            builder.sessionId() != reference["session_id"]
            or builder.userData("cycles_graph_id") != reference["identity"]
        ):
            raise MaterialSourceError("Cycles material identity changed during capture: " + reference["path"])
        layers = reference.get("overrides", ())
        surface_layers = reference.get("surface_overrides", ())
        displacement_layers = reference.get("displacement_overrides", ())
        if not layers and not surface_layers and not displacement_layers:
            result.append(_graph_material(builder, reference["key"], at_time, shutter_times))
            continue
        from ..materials.material_overrides import Evaluation, UnsupportedOverride

        if surface_layers or displacement_layers:
            from ..materials.material_domains import ScopedEvaluation

            evaluation = ScopedEvaluation(builder, layers, surface_layers, displacement_layers)
        else:
            evaluation = Evaluation(builder, layers)
        while True:
            try:
                material = _graph_material(builder, reference["key"], at_time, shutter_times, evaluation)
            except UnsupportedOverride as error:
                evaluation.omit(error)
            else:
                break
        if evaluation.diagnostics:
            material.setdefault("source_diagnostics", []).extend(evaluation.diagnostics)
        result.append(material)
    return result


def material_configuration(rop, now, motion):
    """One owned material/shutter policy for viewer, native IPR and SOHO batch."""
    from functools import partial

    shutter_times = motion_times(motion, now) if motion["enabled"] else None
    return dict(
        materials=[material_definition(rop, now, shutter_times)],
        material_resolver=partial(resolve_materials, shutter_times=shutter_times),
    )


def geometry_for_rop(rop, sop, now, *, interactive=True, diagnostics=None):
    value = lambda name: rop.parm(name).evalAtTime(now)
    mode = "native" if value("use_material_assignments") and not value("materialpath") else "uniform"
    control = value
    subdivision = dict(
        dicing_rate=control("ipr_dicing_rate" if interactive else "dicing_rate"),
        max_level=control("max_subdivisions"),
        triangle_limit=control("subdivision_limit") if control("limit_subdivision") else 0,
    )
    obj = object_for_sop(sop)
    from . import object_properties, stylesheets, instancing

    motion_settings = instancing.motion_settings(obj, now, value("geometry_motion"))
    subdivision = object_properties.subdivision_settings(obj, now, subdivision, interactive=interactive)
    result = geometry_request(
        obj,
        sop,
        material=f"rop:{rop.sessionId()}/surface",
        at_time=now,
        point_radius=object_properties.override(obj, "point_radius", now, value("point_radius")),
        point_render_mode=object_properties.point_render_mode(obj, now),
        curve_width=object_properties.override(obj, "curve_width", now, value("curve_width")),
        curve_segments=object_properties.override(obj, "curve_segments", now, value("curve_segments")),
        motion=motion_settings["motion"],
        volume_step_size=object_properties.override(obj, "volume_step_size", now, value("volume_step_size")),
        material_mode=mode,
        subdivision=subdivision,
        stylesheets=stylesheets.selected(rop, now),
    )
    result["point_splat_default_material"] = not bool(value("materialpath"))
    result.update(motion_settings)
    result["visibility"] = object_scene.object_visibility(obj, now)
    result.update(object_properties.component_settings(obj, now))
    return result


def motion_configuration(rop, now, shutter_frames=None, fps=None):
    if shutter_frames is None:
        camera = hou.node(rop.parm("camera").evalAtTime(now))
        shutter = camera.parm("shutter") if camera else None
        shutter_frames = shutter.evalAtTime(now) if shutter else 0.5
    from ..rop.passes import has

    vectors = has(rop, "motion", now)
    return dict(
        vectors=vectors,
        enabled=bool(rop.parm("allowmotionblur").evalAtTime(now)),
        shutter_frames=shutter_frames,
        fps=hou.fps() if fps is None else fps,
        offset=rop.parm("shutteroffset").evalAtTime(now),
        samples=rop.parm("motion_samples").evalAtTime(now),
    )


def motion_times(settings, now):
    """Seconds, uniformly sampled; +1 places the full interval after now."""
    fps, shutter, offset, samples = (settings[key] for key in ("fps", "shutter_frames", "offset", "samples"))
    if (
        not all(math.isfinite(value) for value in (now, fps, shutter, offset))
        or fps <= 0
        or not 0 <= shutter <= 1
        or not -1 <= offset <= 1
        or not isinstance(samples, int)
        or not 3 <= samples <= 129
        or samples % 2 != 1
    ):
        raise ValueError(
            "Native motion requires positive FPS, shutter in [0,1], offset in [-1,1] and odd 3–129 samples"
        )
    if settings.get("vectors"):
        return [now - 1 / fps, now, now + 1 / fps]
    if not settings["enabled"] or shutter == 0:
        return [now]
    duration = shutter / fps
    centre = now + offset * duration * 0.5
    # Match native_motion_window's operation order exactly: these times also
    # identify the external source-frame defaults captured for each sample.
    result = [now + duration * (offset * 0.5 + step / (samples - 1) - 0.5) for step in range(samples)]
    result[samples // 2] = centre
    if not all(math.isfinite(value) for value in result):
        raise ValueError("Native shutter interval exceeds the supported time range")
    return result


def light_transform(light, now):
    """Requested-frame world transform; analytic lights do not move over the shutter."""
    return matrix_from_houdini(light.worldTransformAtTime(now)) if light else [1, 0, 0, -2, 0, 1, 0, 3, 0, 0, 1, 4]


def light_pose_target_paths(node, now):
    """Absolute OBJ paths used by Houdini's Look At and Follow Path transforms."""
    for name in ("lookatpath", "pathobjpath"):
        path = object_scene.value(node, name, now, "")
        if path:
            yield posixpath.normpath(posixpath.join(node.path(), path))


def film_transparency(rop, now):
    """Evaluate the current live/batch film controls."""
    result = {name: bool(rop_value(rop, name, now)) for name in ("transparent_background", "transparent_glass")}
    result["transparent_roughness_threshold"] = rop_value(rop, "transparent_roughness_threshold", now)
    return result


def source_path(rop, name, now):
    path = rop.parm(name).evalAtTime(now)
    if path.startswith("op:"):
        path = path[3:]
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(rop.path(), path)) if path else ""


def geometry_requests_for_rop(rop, sop, now, *, interactive=True, diagnostics=None, _stack=(), _external_plan=None):
    """Ordinary geometry plus point instancing.

    An Instance OBJ (Point Instancing on/fast/off) renders its prototypes only. Any other
    object whose points carry s@instance instances those points (like Redshift and Octane)
    and renders its other primitives as usual; its points are then not rendered as spheres."""
    from . import instancing

    issues = [] if diagnostics is None else diagnostics
    obj = object_for_sop(sop)
    instancing.reset_owner(rop, obj)
    base = geometry_for_rop(rop, sop, now, interactive=interactive, diagnostics=issues)

    def prototype(rop_, prototype_sop, now_, stack):
        requests = geometry_requests_for_rop(
            rop_,
            prototype_sop,
            now_,
            interactive=interactive,
            diagnostics=issues,
            _stack=stack,
            _external_plan=_external_plan,
        )
        if len(requests) > 1:
            issues.append(
                dict(
                    code="instancing.nested_attribute",
                    source=object_for_sop(prototype_sop).path(),
                    resource=requests[0]["object_key"],
                    message="Points with an instance attribute inside an instanced object are not instanced again",
                    action="Rendering that object's other geometry",
                )
            )
        return requests[0]

    if instancing.is_instancer(obj):
        return [instancing.request(rop, obj, sop, now, base, prototype, issues, _stack, external_plan=_external_plan)]
    base = instancing.packed_request(rop, obj, sop, now, base, issues, external_plan=_external_plan)
    observed = _external_plan.observation(sop) if _external_plan is not None else None
    has_instances = observed[1] if observed is not None else instancing.has_instance_attribute(sop, now)
    if has_instances:
        ordinary = dict(base, render_types=dict(base["render_types"], points=False))
        return [
            ordinary,
            instancing.request(
                rop, obj, sop, now, base, prototype, issues, _stack, attribute_only=True, external_plan=_external_plan
            ),
        ]
    return [base]


def scene_sources(rop, now, motion, deleted_sources=(), *, interactive=True, diagnostics=None):
    """Identical scene selection for the panel, native IPR and SOHO batch."""
    times = motion_times(motion, now)
    diagnostics = [] if diagnostics is None else diagnostics
    from . import instancing

    instancing.reset(rop)

    def geometry(sop, plan):
        results = geometry_requests_for_rop(
            rop, sop, now, interactive=interactive, diagnostics=diagnostics, _external_plan=plan
        )
        obj = object_for_sop(sop)
        for result in results:
            if result["material_mode"] == "native" and result["motion_blur"] and len(times) > 1:
                from .stylesheets import object_binding_samples

                result["object_material_samples"] = object_binding_samples(obj, rop, now, times, result)
            if result["motion_blur"] and any(
                object_scene.object_visibility(obj, sample) != result["visibility"] for sample in times if sample != now
            ):
                raise ValueError(
                    "Animated OBJ ray visibility over the shutter requires a temporal visibility adapter: " + obj.path()
                )
        return results

    if object_scene.enabled(rop, now):
        objects, lights = object_scene.selected(rop, now), object_scene.selected(rop, now, lights=True)
        identities = lambda nodes: tuple(node.sessionId() for node in nodes)
        with instancing.ExternalPlan([obj.renderNode() for obj in objects], now, times, rop=rop) as plan:
            for sample in times:
                if sample == now:
                    continue
                if identities(object_scene.selected(rop, sample)) != identities(objects) or identities(
                    object_scene.selected(rop, sample, lights=True)
                ) != identities(lights):
                    raise ValueError(
                        "Native scene membership changes over the shutter require a temporal visibility adapter"
                    )
            # Light and membership capture remain on Houdini's main thread while
            # the independent Blender process starts and authenticates its pipe.
            definitions = [
                definition
                for light in lights
                if not world.is_environment(light)
                for definition in object_scene.light_definitions(light, now, [now], diagnostics)
            ]
            return dict(
                source_diagnostics=diagnostics,
                geometry=[request for obj in objects for request in geometry(obj.renderNode(), plan)],
                lights=definitions,
            )
    value = lambda name: rop.parm(name).evalAtTime(now)
    was_deleted = lambda path: any(
        path == deleted or path.startswith(deleted.rstrip("/") + "/") for deleted in deleted_sources
    )
    sop_path, light_path = source_path(rop, "soppath", now), source_path(rop, "lightpath", now)
    if any(
        source_path(rop, "soppath", sample) != sop_path or source_path(rop, "lightpath", sample) != light_path
        for sample in times
        if sample != now
    ):
        raise ValueError("Explicit SOP/light sources change over the shutter; this requires a temporal source adapter")
    sop = hou.node(sop_path) if sop_path else None
    light = hou.node(light_path) if light_path else None
    if sop is None and not was_deleted(sop_path):
        raise RuntimeError("Missing geometry SOP: " + value("soppath"))
    if light_path and light is None and not was_deleted(light_path):
        raise RuntimeError("Missing light OBJ: " + value("lightpath"))
    with instancing.ExternalPlan([sop], now, times, rop=rop) as plan:
        definitions = (
            [
                dict(
                    key=f"rop:{rop.sessionId()}/light",
                    kind="point",
                    transform=light_transform(light, now),
                    shadow_catcher=bool(object_scene.value(light, "cycles_light_shadow_catcher", now, True))
                    if light
                    else True,
                    cryptomatte_object=object_scene.value(light, "cycles_cryptomatte_object", now, "") or light.path()
                    if light
                    else rop.path() + "/FallbackLight",
                    cryptomatte_asset=object_scene.value(light, "cycles_cryptomatte_asset", now, "") or light.path()
                    if light
                    else rop.path() + "/FallbackLight",
                    linking_id=light.path() if light else f"rop:{rop.sessionId()}/light",
                    source_path=light.path() if light else rop.path(),
                    strength=rop.parmTuple("lightstrength").evalAtTime(now),
                    lightgroup=object_scene.value(light, "cycles_lightgroup", now, "") if light else "",
                )
            ]
            if light is not None or not light_path
            else []
        )
        return dict(source_diagnostics=diagnostics, geometry=geometry(sop, plan) if sop else [], lights=definitions)


def tracking_paths(rop, now):
    if object_scene.enabled(rop, now):
        return (
            " ".join(node.path() for node in object_scene.selected(rop, now)),
            " ".join(node.path() for node in object_scene.selected(rop, now, lights=True)),
        )
    sop = rop.node(rop.parm("soppath").evalAtTime(now))
    light = rop.node(rop.parm("lightpath").evalAtTime(now)) if rop.parm("lightpath").evalAtTime(now) else None
    return object_for_sop(sop).path() if sop else "", light.path() if light else ""


def scene_lighting_configuration(rop, now, motion, deleted_sources=(), *, interactive=True, diagnostics=None):
    issues = [] if diagnostics is None else diagnostics
    scene = scene_sources(rop, now, motion, deleted_sources, interactive=interactive, diagnostics=issues)
    from ..blender import blender_assets
    from . import instancing

    blender_assets.retain_prototypes(rop, instancing.prepared_keys(rop))
    scene["_blender_prototypes"] = dict(rop=rop.sessionId(), keys=list(instancing.prepared_keys(rop)))
    cache = blender_assets.cache_diagnostics()["prototypes"]
    if cache["active_over_budget_bytes"] > 0:
        mib = 1024 * 1024
        largest = ", ".join(
            asset["asset"] + " (" + format(asset["bytes"] / mib, ".1f") + " MiB)"
            for asset in cache["largest_assets"][:3]
        )
        issues.append(
            dict(
                code="blend.active_cache_over_budget",
                severity="warning",
                source=rop.path(),
                resource=f"rop:{rop.sessionId()}",
                message="Active Blender prototypes use "
                + format(cache["active_bytes"] / mib, ".1f")
                + " MiB ("
                + format(cache["resident_bytes"] / mib, ".1f")
                + " MiB resident) against a cache target of "
                + format(cache["target_bytes"] / mib, ".1f")
                + " MiB. Largest assets: "
                + largest,
                action="Retaining all displayed prototypes; reduce active Blender frame sets or increase the cache target",
            )
        )
    environment = world.configuration(rop, now, motion_times(motion, now), diagnostics=issues)
    from . import linking

    return dict(**scene, **environment, linking=linking.configuration(scene, environment, now, issues))


def dicing_configuration(rop, now, *, render_camera_path=None, interactive=True):
    path = rop.evalParm("dicing_camera")
    selected = rop.node(path) if path else None
    render = rop.node(rop.evalParm("camera") if render_camera_path is None else render_camera_path)
    # Selecting the active render camera explicitly has the same tracking
    # semantics as leaving the selector empty, including camera-only updates.
    from ..ipr import registry

    entry = registry.get(f"rop:{rop.sessionId()}") if interactive else None
    return dict(
        offscreen_scale=rop.parm("offscreen_dicing_scale").evalAtTime(now),
        update=bool(
            not interactive or not rop.parm("tessellation_update") or rop.parm("tessellation_update").evalAtTime(now)
        ),
        refresh_revision=entry.tessellation_revision if entry else 0,
        camera_path="" if selected is not None and selected == render else selected.path() if selected else path,
    )


def capture(rop, at_time=None, deleted_sources=(), *, camera_only=False):
    from ..rop.passes import configuration as pass_configuration, custom_configuration

    now = hou.time() if at_time is None else at_time
    value = lambda name: rop.parm(name).evalAtTime(now)
    from . import camera_properties

    motion = motion_configuration(rop, now)
    dof = camera_properties.configuration(rop, now, motion_times(motion, now) if motion["enabled"] else [now])
    sampling = camera_properties.sampling_configuration(rop, now, motion)
    if camera_only:
        # Notifications establish that no scene domain except the active camera
        # changed. Do not traverse objects, SOPs, shader graphs or textures.
        return dict(
            camera_path=value("camera"),
            camera_dof=dof,
            camera_sampling=sampling,
            motion=motion,
            pixel_aspect=value("pixel_aspect"),
        )
    from ..rop import output

    region = output.crop(rop, now, *output.dimensions(rop, now))
    material = f"rop:{rop.sessionId()}/surface"
    issues = []
    return dict(
        **scene_lighting_configuration(rop, now, motion, deleted_sources, diagnostics=issues),
        crop=region,
        motion=motion,
        passes=pass_configuration(rop, now),
        **custom_configuration(rop, now),
        denoising=denoising_configuration(rop, now),
        rendering=rendering_configuration(rop, now),
        **film_transparency(rop, now),
        dicing=dicing_configuration(rop, now),
        camera_path=value("camera"),
        camera_dof=dof,
        camera_sampling=sampling,
        pixel_aspect=value("pixel_aspect"),
        background=rop.parmTuple("background").evalAtTime(now),
        **material_configuration(rop, now, motion),
    )


def camera_signature(update, now):
    """Public host state for scheduling only; HDK owns camera conversion."""
    if "camera" in update:
        return (update["camera"], update.get("camera_dof"), update.get("camera_sampling"))
    path = update.get("camera_path")
    if path is None:
        return None
    camera = hou.node(path)
    if camera is None:
        return ("missing", path)  # The capture adapter supplies the diagnostic.
    motion = update.get("motion", {})
    times = motion_times(motion, now) if motion else [now]

    # A centre-only signature loses shutter-only lens edits and nominal fallback
    # changes under offset shutters. These are parameter reads, never SOP cooks.
    def stable(value):
        return ("nonfinite", str(value)) if isinstance(value, float) and not math.isfinite(value) else value

    def freeze(value):
        if isinstance(value, dict):
            return tuple((k, freeze(v)) for k, v in sorted(value.items()))
        if isinstance(value, (list, tuple)):
            return tuple(freeze(v) for v in value)
        return stable(value)

    def parameters(t):
        from .camera_properties import panorama_source

        def value(name, default):
            return camera.parm(name).evalAtTime(t) if camera.parm(name) else default

        override = value("cycles_projection", "native")
        if override != "native":
            return (
                ("near", stable(value("near", 0.01))),
                ("far", stable(value("far", 10000))),
                ("cycles_projection", override),
                ("panorama", freeze(panorama_source(value))),
            )
        projection = camera.parm("projection").evalAtTime(t) if camera.parm("projection") else 0
        names = ("near", "far", "projection") + (("orthowidth",) if projection == 1 else ("focal", "aperture"))
        return tuple((name, stable(camera.parm(name).evalAtTime(t))) for name in names if camera.parm(name))

    intrinsics = tuple((t, parameters(t)) for t in sorted(set([now] + times)))
    return (
        camera.sessionId(),
        intrinsics,
        update.get("camera_dof"),
        update.get("camera_sampling"),
        tuple(tuple(stable(v) for v in camera.worldTransformAtTime(t).asTuple()) for t in times),
    )


def _observable_source_node(node):
    """Watch a locked asset's public boundary, retaining editable internals.

    Cook scripts may rewrite private SOP/DOP parameters even when their values
    are unchanged. Those implementation notifications are not artist edits;
    subscribing to them can make every capture queue another capture. Public
    asset parameters, upstream sources and explicitly editable contents still
    participate in normal scene observation.
    """
    while node is not None and node.isInsideLockedHDA() and not node.isEditableInsideLockedHDA():
        node = node.parent()
    return node


# CYCLES_HOUDINI_LIGHT_RULE=legacy restores the previous allow-list (48f5a4f) for comparisons.
_LEGACY_LIGHT_PARMS = (
    frozenset(
        (
            "light_intensity",
            "light_exposure",
            "light_color",
            "cycles_light_color",
            "cycles_light_use_temperature",
            "cycles_light_temperature",
            "l_dimmer",
            "dimmer",
        )
    )
    if os.environ.get("CYCLES_HOUDINI_LIGHT_RULE") == "legacy"
    else None
)


# Parameters that move the light's OBJ transform (and so its parented OBJ children).
_TRANSFORM_PARMS = frozenset(("t", "r", "s", "p", "pr", "scale", "xOrd", "rOrd", "pre_xform", "constraints_on"))


def _local_expression(expression, language):
    """True if an expression reads only time and the node's own channels.

    HScript: no absolute, parent or relative node path ('./name' channel reads are
    allowed), no op: references, no backtick evaluation. Python: only hou.frame(),
    hou.time(), hou.fps(), hou.intFrame() and hou.pwd() channel reads; anything
    else could reach another node and cannot be proven local.
    """
    if language == hou.exprLanguage.Python:
        import re

        calls = re.findall(r"hou\.([A-Za-z_]+)", expression)
        allowed = {"frame", "time", "fps", "intFrame", "pwd"}
        if any(call not in allowed for call in calls):
            return False
        if "parent" in expression or "node(" in expression or "/" in expression.replace("'./", "").replace('"./', ""):
            return False
        return True
    text = expression.replace('"./', '"').replace("'./", "'")
    return not any(token in text for token in ("/", "op:", "`"))


def _local_light_parm(parm, node):
    """True if this parameter's value depends only on the light node itself and time.

    A plain value, keyframes (animation is time, evaluated at the current frame by
    the same code as the complete read), a channel reference into the same light,
    or expressions that read only time and the light's own channels. A reference
    to another node (channel reference elsewhere, an object path) is not local.
    """
    referenced = parm.getReferencedParm()
    if referenced != parm:
        return referenced.node() == node and _local_light_parm(referenced, node)
    template = parm.parmTemplate()
    if template.type() == hou.parmTemplateType.String and template.stringType() == hou.stringParmType.NodeReference:
        # A path to another OBJ (look-at, follow path, constraints) makes the light's
        # transform depend on that node: complete read. A material path is the
        # light's own shader and stays local.
        target = node.node(parm.evalAsString()) if parm.evalAsString() else None
        if target is not None and target.type().category() == hou.objNodeTypeCategory():
            return False
    for key in parm.keyframes():
        try:
            expression, language = key.expression(), key.expressionLanguage()
        except hou.KeyframeValueNotSet:
            continue
        if not _local_expression(expression, language):
            return False
    return True


def _light_material_paths(lights, now):
    from .light_shaders import material_path

    return tuple(material_path(light, now) for light in lights)


def light_basis(rop, now, update):
    """Accepted scene inputs for partial reads of lights and linking."""
    from . import linking

    try:
        if not object_scene.enabled(rop, now):
            return None
        lights = object_scene.selected(rop, now, lights=True)
        return dict(
            update=update,
            root_id=object_scene.root(rop, now).sessionId(),
            geometry_ids=tuple(sorted(node.sessionId() for node in object_scene.selected(rop, now))),
            light_ids=tuple(light.sessionId() for light in lights),
            environment_ids=tuple(sorted(light.sessionId() for light in lights if world.is_environment(light))),
            light_materials=_light_material_paths(lights, now),
            light_selectors=linking.light_selector_signature(update.get("lights", ()), update),
        )
    except (ValueError, hou.Error):
        return None


def light_capture(rop, now, motion, basis):
    """Lights-only read after an edit on a light node: (update, path) or (None, reason).

    Rebuilds every light definition, the environment and light linking with the
    same code as the complete read, then checks the light's footprint outside the
    lights against the accepted scene (basis). The update carries the lights and
    only those world/linking values that changed. A complete read is required when
    the edit changes object membership, makes a light geometry, or produces any
    diagnostic (scene-wide diagnostics belong to the complete read).
    """
    if basis is None:
        return None, "no accepted OBJ-scene read"
    from . import linking

    try:
        if not object_scene.enabled(rop, now):
            return None, "SOP-mode scene"
        # Object membership is not read again here: an edit that changes it (flags,
        # masks, creation) reaches the observer as a non-light notification, which
        # revokes the lights-only read. Lights also selected as geometry were
        # excluded from eligibility (SceneObserver._light_value_nodes).
        geometry_ids = basis["geometry_ids"]
        lights = object_scene.selected(rop, now, lights=True)
        light_ids = tuple(light.sessionId() for light in lights)
        if any(identity in geometry_ids for identity in light_ids):
            return None, "a light is also rendered as geometry"
        issues = []
        definitions = [
            definition
            for light in lights
            if not world.is_environment(light)
            for definition in object_scene.light_definitions(light, now, [now], issues)
        ]
        if issues:
            return None, "light diagnostic: " + ", ".join(sorted({str(i.get("code", "?")) for i in issues}))
        accepted = basis["update"]
        result = dict(lights=definitions)
        path = "lights"
        environment_ids = tuple(sorted(light.sessionId() for light in lights if world.is_environment(light)))
        if environment_ids or basis["environment_ids"]:
            environment = world.configuration(rop, now, motion_times(motion, now), diagnostics=issues)
            if issues:
                return None, "environment diagnostic: " + ", ".join(sorted({str(i.get("code", "?")) for i in issues}))
            for name, value in environment.items():
                if accepted.get(name) != value:
                    result[name] = value
                    path = "lights+world"
        else:
            environment = {name: accepted[name] for name in ("world",) if name in accepted}
        light_selectors = linking.light_selector_signature(definitions, environment)
        literal_geometry_selectors = basis.get("literal_geometry_selectors")
        if literal_geometry_selectors is None and light_selectors is not None:
            # Establish this only when a light edit can use it. Complete reads
            # (including each new frame) already do the authoritative linking read.
            literal_geometry_selectors = linking.geometry_selectors_are_literal(accepted.get("geometry", ()))
        if not literal_geometry_selectors or light_selectors is None or light_selectors != basis.get("light_selectors"):
            light_links = linking.configuration(
                dict(geometry=accepted.get("geometry", []), lights=definitions), environment, now, issues
            )
            if issues:
                return None, "linking diagnostic"
            if light_links != accepted.get("linking"):
                result["linking"] = light_links
                path += "+linking"
        materials = _light_material_paths(lights, now)
        # Subscriptions only need rebuilding for a light material not watched yet
        # (a removed or disabled light leaves nothing new to observe).
        if set(materials) - set(basis["light_materials"]) - {""}:
            result["_refresh_observer"] = True
        # The next basis, without reading the scene again (Entry.apply).
        result["_basis"] = dict(
            root_id=basis["root_id"],
            geometry_ids=geometry_ids,
            light_ids=light_ids,
            environment_ids=environment_ids,
            light_materials=materials,
            literal_geometry_selectors=literal_geometry_selectors,
            light_selectors=light_selectors,
        )
    except (ValueError, hou.Error) as error:
        return None, "read failed: " + str(error)[:120]
    return result, path


def receiver_linking_capture(rop, now, motion, basis, receiver_ids):
    """Re-resolve linking using the accepted object identities and light definitions."""
    if basis is None:
        return None
    from . import linking

    accepted = basis["update"]
    if any(issue.get("code") == "linking.source_unavailable" for issue in accepted.get("source_diagnostics", ())):
        return None  # A complete read republishes scene-wide source diagnostics.
    try:
        paths = {request["object_path"] for request in accepted["geometry"]}
        for identity in receiver_ids:
            receiver = hou.nodeBySessionId(identity)
            if receiver is None or receiver.path() not in paths:
                return None
        for sample in set([now, *motion_times(motion, now)]):
            if not object_scene.enabled(rop, sample) or object_scene.root(rop, sample).sessionId() != basis["root_id"]:
                return None
            objects, lights = object_scene.selection_signature(rop, sample)
            if objects != basis["geometry_ids"] or lights != basis["light_ids"]:
                return None
        issues = []
        links = linking.configuration(accepted, accepted, now, issues)
        if not issues:
            # Empty receiver and blocker lists also replace the worker's old definition.
            return dict(linking=links, _basis=basis)
    except (ValueError, hou.Error):
        pass
    return None


def _authored_value(parm):
    """What an edit of this parameter changes: the plain value or expression text (rawValue), and for a
    keyframed parameter its channel value now (editing a key changes the value, not the segment function)."""
    raw = parm.rawValue()
    if isinstance(raw, str) and raw.endswith(")") and _keyed(parm.keyframes()):
        return ("keyed", parm.eval())
    return raw


# Houdini's channel segment functions: a parameter whose keyframes use these is animated by keys
# (its authored value is the key values; compare the channel value). Any other keyframe expression
# (ch(), Python, ...) is an expression parameter: compare its text, never evaluate it in an event.
_SEGMENT_FUNCTION = re.compile(
    r"^\s*(bezier|linear|qlinear|constant|cubic|spline|quintic|ease|easein|easeout|easep|easeinp|easeoutp"
    r"|cycle|cyclet|cycleoffset|cycleoffsett|repeat|repeatt|match|matchin|matchout|vmatch|vmatchin|vmatchout)"
    r"\s*\([^()]*\)\s*$"
)
_TEMPLATE_KINDS = None


def _keyed(keys):
    """True when the parameter is animated by keys with segment functions (not an expression alone)."""
    return any(_SEGMENT_FUNCTION.match(key.expression()) for key in keys if key.isExpressionSet())


def _tuple_kind(tuple_):
    """How to fingerprint a parameter tuple, decided once per baseline (_tuple_fingerprint):
    "plain" numbers and plain text (tuple.eval(): no expression to cook), "expression" (authored text per
    parm: ch(), Python, backtick strings), "keyed" (channel value), "raw" (rawValue: ramps, data and other
    templates), "skip" (folders, buttons, labels, separators: editor state, no value)."""
    global _TEMPLATE_KINDS
    if _TEMPLATE_KINDS is None:
        t = hou.parmTemplateType
        _TEMPLATE_KINDS = {
            t.Float: "plain",
            t.Int: "plain",
            t.Toggle: "plain",
            t.Menu: "plain",
            t.String: "plain",
            t.FolderSet: "skip",
            t.Button: "skip",
            t.Label: "skip",
            t.Separator: "skip",
        }
    template_type = tuple_.parmTemplate().type()
    kind = _TEMPLATE_KINDS.get(template_type, "raw")
    if template_type == hou.parmTemplateType.Folder:
        kind = "raw" if tuple_.isMultiParmParent() else "skip"
    if kind == "skip":
        return kind
    # The HOM iterator indexes the tuple through a Python wrapper per component.
    components = range(len(tuple_))
    for index in components:
        keys = tuple_[index].keyframes()
        if keys:
            return "keyed" if _keyed(keys) else "expression"
    if template_type == hou.parmTemplateType.String:
        if any("`" in tuple_[index].unexpandedString() for index in components):
            return "expression"  # Backtick expressions: compare the text, never evaluate them in an event.
    return kind


def _raw_values(tuple_):
    return tuple(parm.rawValue() for parm in tuple_)


def _keyed_values(tuple_):
    return ("keyed",) + tuple_.eval()


_EVALUATORS = dict(plain=hou.ParmTuple.eval, keyed=_keyed_values, expression=_raw_values, raw=_raw_values)


def _tuple_fingerprint(tuple_, kind):
    return _EVALUATORS[kind](tuple_)


def _material_wiring(builder):
    """The structure of a material network: every node's inputs and bypass flag (no values, no cooking)."""
    if builder is None:
        return None
    rows = []
    for node in (builder, *builder.allSubChildren()):
        connections = []
        for connection in node.inputConnections():
            source = connection.inputNode()
            connections.append(
                (
                    connection.inputIndex(),
                    source.path() if source is not None else connection.inputName(),
                    connection.outputIndex(),
                )
            )
        bypassed = node.isBypassed() if hasattr(node, "isBypassed") else False
        rows.append((node.path(), bypassed, tuple(connections)))
    return tuple(rows)


class SceneObserver:
    """Coalesce public Houdini node notifications; no geometry reads in callbacks."""

    events = (
        hou.nodeEventType.ParmTupleChanged,
        hou.nodeEventType.InputDataChanged,
        hou.nodeEventType.ParmTupleAnimated,
        hou.nodeEventType.ParmTupleChannelChanged,
        hou.nodeEventType.InputRewired,
        hou.nodeEventType.NameChanged,
        hou.nodeEventType.BeingDeleted,
        hou.nodeEventType.FlagChanged,
    )
    object_events = (*events, hou.nodeEventType.SpareParmTemplatesChanged)
    network_events = (hou.nodeEventType.ChildCreated, hou.nodeEventType.ChildDeleted)

    def __init__(
        self,
        rop,
        callback,
        material_paths=None,
        track_deletions=False,
        camera_callback=None,
        object_pose_callback=None,
        stylesheet_resources=None,
        cop_sources=None,
        object_pose_source_callback=None,
        light_value_callback=None,
        material_value_callback=None,
        linking_value_callback=None,
    ):
        self.rop, self.callback, self.nodes = rop, callback, []
        self.material_value_callback = material_value_callback
        self._material_wiring = {}
        self._rop_identity = rop.sessionId()
        self._closed = True
        self.material_paths = material_paths
        self.stylesheet_resources = stylesheet_resources
        self.cop_sources = cop_sources
        self.track_deletions = track_deletions
        self.camera_callback = camera_callback
        self.object_pose_callback = object_pose_callback
        self.object_pose_source_callback = object_pose_source_callback
        self.light_value_callback = light_value_callback
        self.linking_value_callback = linking_value_callback
        self.light_value_nodes = set()
        self.light_refusal = None
        self.deleted_sources = set()
        self.signature = None
        self._material_capture_signature = None
        self._watched_material_paths = None
        self._watched_style_nodes = ()
        self._callback = self._changed
        self.refresh()

    def refresh(self, material_paths=None):
        self.close()
        if hou.nodeBySessionId(self._rop_identity) is None:
            return
        # Retain deletion evidence only for the two currently configured
        # explicit sources that are still absent. This keeps later camera
        # edits valid without accumulating a history of deleted node paths.
        missing = [source_path(self.rop, name, hou.time()) for name in ("soppath", "lightpath")]
        missing = [path for path in missing if path and hou.node(path) is None]
        self.deleted_sources = {
            deleted
            for deleted in self.deleted_sources
            if any(path == deleted or path.startswith(deleted.rstrip("/") + "/") for path in missing)
        }
        if object_scene.enabled(self.rop, hou.time()):
            self.deleted_sources.clear()
        candidates = {self.rop}
        self.non_camera_dependencies = set()
        cop_candidates = set()
        camera = self.rop.node(self.rop.evalParm("camera"))
        networks = {self.rop.parent()}
        sources = [
            (self.rop.evalParm(name), name in ("materialpath", "world_material"))
            for name in ("soppath", "camera", "dicing_camera", "lightpath", "materialpath", "world_material")
            if self.rop.parm(name)
        ]
        if object_scene.enabled(self.rop, hou.time()):
            sources.append((object_scene.value(self.rop, "objectroot", hou.time(), "/obj"), False))
            try:
                network = object_scene.root(self.rop, hou.time())
                networks.add(network)
                observed = set(object_scene.candidates(self.rop, hou.time()))
                observed.update(object_scene.selected(self.rop, hou.time()))
                lights = set(object_scene.selected(self.rop, hou.time(), lights=True))
                observed.update(lights)
                aim_sources = set(lights)
                for obj in observed:
                    ancestors = obj.inputAncestors()
                    candidates.add(obj)
                    candidates.update(ancestors)
                    from .light_shaders import material_path

                    light_material = material_path(obj, hou.time())
                    if light_material:
                        sources.append((light_material, True))
                    if obj != camera:
                        self.non_camera_dependencies.add(obj)
                        self.non_camera_dependencies.update(ancestors)
                    if obj in lights:
                        aim_sources.update(ancestors)
                    if obj.isNetwork():
                        networks.add(obj)
                        # Watch output flag switches before a different SOP is
                        # selected. Bulk geometry is still read only by HDK.
                        candidates.update(
                            child for child in obj.children() if child.type().category() == hou.sopNodeTypeCategory()
                        )
                    sop = obj.renderNode()
                    if sop and sop.type().category() == hou.sopNodeTypeCategory():
                        sources.append((sop.path(), False))
                for obj in aim_sources:
                    for path in light_pose_target_paths(obj, hou.time()):
                        sources.append((path, False))
                        target = hou.node(path)
                        if target is not None:
                            self.non_camera_dependencies.add(target)
                            self.non_camera_dependencies.update(target.inputAncestors())
            except (ValueError, hou.OperationFailed):
                pass  # Preserve recovery observation for a missing scene root.
        elif object_scene.value(self.rop, "world_source", hou.time(), "scene") == "scene":
            # Explicit SOP geometry still receives the selected OBJ environment.
            # Observe disabled environments too: enabling one must update IPR.
            sources.append((object_scene.value(self.rop, "objectroot", hou.time(), "/obj"), False))
            try:
                networks.add(object_scene.root(self.rop, hou.time()))
                for obj in object_scene.candidates(self.rop, hou.time()):
                    if world.is_environment(obj):
                        candidates.add(obj)
                        candidates.update(obj.inputAncestors())
                    if obj.childTypeCategory() == hou.objNodeTypeCategory():
                        networks.add(obj)
            except (ValueError, hou.OperationFailed):
                pass
        if self.material_paths:
            paths = tuple(self.material_paths()) if material_paths is None else material_paths
            self._watched_material_paths = paths
            sources.extend((path, True) for path in paths)
        resources = self.stylesheet_resources() if self.stylesheet_resources else ()
        self._watched_style_nodes = _stylesheet_nodes(resources)
        sources.extend((path, True) for path in self._watched_style_nodes)
        # Explicit SOP-mode light sources also observe assigned emission graphs.
        from .light_shaders import material_path

        for path, is_material in tuple(sources):
            source = hou.node(path) if path.startswith("/") else self.rop.node(path) if path else None
            if source is not None:
                light_material = material_path(source, hou.time())
                if light_material:
                    sources.append((light_material, True))
        for path, is_material in sources:
            if path.startswith("op:"):
                path = path[3:]
            if path and not path.startswith("/"):
                path = posixpath.normpath(posixpath.join(self.rop.path(), path))
            node = hou.node(path) if path else None
            parent_path = posixpath.dirname(path)
            while parent_path and hou.node(parent_path) is None:
                parent_path = posixpath.dirname(parent_path)
            if parent_path:
                networks.add(hou.node(parent_path))
            if node:
                candidates.add(node)
                if node != camera and not is_material:
                    # InputDataChanged excludes expression references. Gather
                    # public reference dependencies after the supported capture
                    # has cooked them, including through SOP subnets. A camera
                    # used by geometry or a shader is not a camera-only edit.
                    self.non_camera_dependencies.add(node)
                    dependencies = node.inputAncestors(follow_subnets=True)
                    self.non_camera_dependencies.update(dependencies)
                    candidates.update(dependencies)
                if node.type().category() == hou.objNodeTypeCategory():
                    candidates.update(node.inputAncestors())
                if is_material:
                    children = node.allSubChildren()
                    candidates.update(children)
                    from ..materials import execution_nodes

                    try:
                        executing = execution_nodes(node)
                    except (ValueError, hou.Error):
                        executing = set()
                    cop_candidates.update(executing)
                    # Keep wiring/flags and non-COP expressions observed on all
                    # children: material overrides may activate other branches.
                    # COP dependencies come from accepted resource demand below.
                    for child in (node, *children):
                        for dependency in child.references(include_children=False):
                            category = dependency.type().category().name().lower()
                            if "cop" in category:
                                continue
                            self.non_camera_dependencies.add(dependency)
                            candidates.add(dependency)
                            dependencies = dependency.inputAncestors(follow_subnets=True)
                            self.non_camera_dependencies.update(dependencies)
                            candidates.update(dependencies)
                    networks.add(node)
                    networks.update(child for child in node.allSubChildren() if child.isNetwork())
                if node.type().category() == hou.sopNodeTypeCategory():
                    candidates.update(node.inputAncestors())
                    candidates.add(object_for_sop(node))
        # Point-instancing prototypes found by the latest capture: their OBJ, SOP network
        # and, for missing references, the nearest existing parent (recreation repairs).
        from . import instancing

        self._watched_instancing = instancing.dependencies(self.rop)
        self.instancing_prototypes = set()
        for path in self._watched_instancing[0]:
            node = hou.node(path)
            if node is None or instancing.is_generated(node):
                continue
            self.instancing_prototypes.add(node)
            dependencies = {node, *node.inputAncestors()}
            sop = node.renderNode()
            if sop is not None:
                dependencies.add(sop)
                dependencies.update(sop.inputAncestors(follow_subnets=True))
            candidates.update(dependencies)
            self.non_camera_dependencies.update(dependencies)
            networks.add(node)
            networks.add(node.parent())
        for path in self._watched_instancing[1]:
            invalid = hou.node(path)
            if invalid is not None:
                candidates.add(invalid)
                self.non_camera_dependencies.add(invalid)
                if invalid.isNetwork():
                    networks.add(invalid)
                    # Flagging an already-created SOP as the render source is also a repair.
                    candidates.update(invalid.allSubChildren())
                    self.non_camera_dependencies.update(invalid.allSubChildren())
                    networks.update(n for n in invalid.allSubChildren() if n.isNetwork())
            parent_path = posixpath.dirname(path)
            while parent_path and hou.node(parent_path) is None:
                parent_path = posixpath.dirname(parent_path)
            if parent_path:
                networks.add(hou.node(parent_path))
        # Subscribe to referenced COP inputs and project settings without
        # resolving/cooking pixels. Missing operator parents remain watched so
        # recreation repairs the accepted material automatically.
        from .. import _native

        if self.cop_sources is not None:
            accepted_sources = self.cop_sources()
        else:
            accepted_sources = None
        self._cop_sources_known = accepted_sources is not None
        cop_sources = set(accepted_sources or ())
        if accepted_sources is None:
            for node in tuple(
                cop_candidates | {n for n in candidates if n.type().category() != hou.vopNodeTypeCategory()}
            ):
                for name in ("filename", "env_map", "cycles_light_texture", "areamap"):
                    parm = node.parm(name)
                    if parm is None:
                        continue
                    source = parm.evalAsString()
                    if source.startswith("op:"):
                        cop_sources.add(source)
        self._watched_cop_sources = tuple(sorted(cop_sources))
        for source in self._watched_cop_sources:
            info = _native.cop_image_source_info(source)
            for identity in info["nodes"]:
                dependency = hou.nodeBySessionId(identity)
                if dependency:
                    candidates.add(dependency)
                    self.non_camera_dependencies.add(dependency)
                    if dependency.isNetwork():
                        networks.add(dependency)
            parent_path = source[3:].split("[")[0].rsplit("/", 1)[0]
            while parent_path and hou.node(parent_path) is None:
                parent_path = parent_path.rsplit("/", 1)[0]
            if parent_path:
                networks.add(hou.node(parent_path))
        # OBJ subnet containment contributes to world transforms as well as
        # wired OBJ parents. Observe those containers in explicit SOP/light
        # mode too, where they need not appear in scene-selection candidates.
        for source in tuple(candidates):
            if source is None or source.type().category() != hou.objNodeTypeCategory():
                continue
            parent = source.parent()
            while parent is not None and parent.type().category() == hou.objNodeTypeCategory():
                ancestors = {parent, *parent.inputAncestors()}
                candidates.update(ancestors)
                if source != camera:
                    self.non_camera_dependencies.update(ancestors)
                parent = parent.parent()
        generated = instancing.generated_nodes(candidates | networks)
        candidates = {
            _observable_source_node(node) for node in candidates if node is not None and node not in generated
        }
        networks = {_observable_source_node(node) for node in networks if node is not None and node not in generated}
        object_category = hou.objNodeTypeCategory()
        parameter_nodes = []
        for node in candidates:
            if node is None:
                continue
            events = self.events
            if node.type().category() == object_category:
                events = self.object_events
                parameter_nodes.append(node)
            node.addEventCallback(events, self._callback)
            self.nodes.append((node, events))
        for node in networks:
            if node:
                node.addEventCallback(self.network_events, self._callback)
                self.nodes.append((node, self.network_events))
                if node.type().category() == object_category:
                    parameter_nodes.append(node)
        from . import linking

        self.light_value_nodes = self._light_value_nodes()
        # Structure of the rendered material networks: a parameter edit that also reports InputRewired /
        # InputDataChanged downstream is still a value edit while this is unchanged (_material_value_edit).
        self._material_wiring = {}
        if self.material_value_callback is not None:
            for path in self._watched_material_paths or ():
                try:
                    self._material_wiring[path] = _material_wiring(hou.node(path))
                except hou.Error:
                    pass
        # Baseline of the lights' non-transform parameters: a handle drag reports t and r together
        # without a tuple; _unnamed_tuple_changes then classifies it (CYCLES_HOUDINI_UNNAMED_TUPLE_RULE=0: off).
        self.remember_parameters(
            [n if isinstance(n, hou.Node) else hou.nodeBySessionId(n) for n in self.light_value_nodes] + parameter_nodes
        )
        self._bundle_references = linking.bundle_references(candidates, hou.time())
        self.signature = self._selection_signature()
        self._closed = False

    def _light_value_nodes(self):
        """Light nodes whose parameter edits may take the lights-only read.

        Every OBJ light of the scene root (also disabled or masked ones: enabling
        or including them is a lights-only change), analytic or environment, that
        no other source reads, apart from literal linking selectors. Expressions
        elsewhere, or external uses of the light's guide, require a complete
        read. Lights also rendered as geometry are excluded.
        """
        try:
            if not object_scene.enabled(self.rop, hou.time()):
                return set()
            geometry = {node.sessionId() for node in object_scene.selected(self.rop, hou.time())}
            candidates = object_scene.candidates(self.rop, hou.time())
        except (ValueError, hou.OperationFailed):
            return set()
        from . import linking
        from .light_properties import supported

        result = set()
        for light in candidates:
            try:
                if light.sessionId() in geometry or light in self.instancing_prototypes:
                    continue
                if not supported(light):
                    continue
                prefix = light.path().rstrip("/") + "/"
                direct = set(light.dependents(include_children=False))
                if all(
                    dependent == light
                    or dependent.path().startswith(prefix)
                    or (dependent in direct and linking.references_only_by_linking(dependent, light))
                    for dependent in light.dependents(include_children=True)
                ):
                    result.add(light.sessionId())
            except hou.ObjectWasDeleted:
                continue
        return result

    def _light_parm_edit(self, node, parm_tuple, kwargs, transform=False):
        """An edit on an eligible light node whose new value is the light's own.

        A light-node edit that is refused records why in light_refusal (trace/tests).
        """
        if kwargs.get("event_type") not in (
            hou.nodeEventType.ParmTupleChanged,
            hou.nodeEventType.ParmTupleAnimated,
            hou.nodeEventType.ParmTupleChannelChanged,
        ):
            return False
        if node.sessionId() not in self.light_value_nodes:
            return False
        reason = ""
        prefix = node.path().rstrip("/") + "/"
        if _LEGACY_LIGHT_PARMS is not None and (transform or parm_tuple.name() not in _LEGACY_LIGHT_PARMS):
            reason = "legacy allow-list"
        elif parm_tuple.name() in ("lookatpath", "pathobjpath", "constraints_on"):
            reason = "a light's transform dependencies changed"
        elif (transform or parm_tuple.name() in _TRANSFORM_PARMS) and any(
            not child.path().startswith(prefix) for child in node.outputs()
        ):
            reason = "OBJ children follow the light's transform"
        elif not all(_local_light_parm(parm, node) for parm in parm_tuple):
            reason = "value depends on something outside the light (animation, expression, path to an object)"
        elif any(
            other.node() != node and not other.node().path().startswith(prefix)
            for parm in parm_tuple
            for other in parm.parmsReferencingThis()
        ):
            reason = "another node references this parameter"
        if reason:
            self.light_refusal = dict(node=node.path(), parm=parm_tuple.name(), reason=reason)
            return False
        return True

    def _receiver_mask_edit(self, node, parm_tuple, event):
        """Qualify a plain native geometry Light Mask edit using Houdini's live dependency graph."""
        if (
            event != hou.nodeEventType.ParmTupleChanged
            or parm_tuple is None
            or parm_tuple.name() != "lightmask"
            or len(parm_tuple) != 1
            or node.type().category() != hou.objNodeTypeCategory()
            or node.type().name() != "geo"
            or self._bundle_references
        ):
            return False
        from . import linking

        parm = parm_tuple[0]
        template = parm.parmTemplate()
        return (
            template.type() == hou.parmTemplateType.String
            and template.stringType() == hou.stringParmType.NodeReferenceList
            and not template.scriptCallback()
            and not parm.keyframes()
            and linking._literal_fields(node, ("lightmask",)) is not None
            and not parm.parmsReferencingThis()
            and all(
                linking.references_only_by_linking(dependent, node)
                for dependent in node.dependents(include_children=False)
            )
        )

    def _selection_signature(self):
        try:
            signature = object_scene.selection_signature(self.rop, hou.time())
            if signature is None and object_scene.value(self.rop, "world_source", hou.time(), "scene") == "scene":
                signature = (
                    "sop-environments",
                    tuple(
                        light.sessionId()
                        for light in object_scene.selected(self.rop, hou.time(), lights=True)
                        if world.is_environment(light)
                    ),
                )
            from . import linking, stylesheets, instancing

            resources = self.stylesheet_resources() if self.stylesheet_resources else ()
            return (
                signature,
                linking.bundle_signature(getattr(self, "_bundle_references", ())),
                stylesheets.signature(self.rop, hou.time(), resources),
                instancing.external_signature(self.rop),
            )
        except (ValueError, hou.OperationFailed) as error:
            return ("error", str(error))

    def selection_changed(self):
        if self._closed:
            return
        signature = self._selection_signature()
        if signature != self.signature:
            self.signature = signature
            self.callback()

    def refresh_material_bindings(self, session):
        if self._closed:
            return
        if self.cop_sources is not None:
            sources = self.cop_sources()
            if sources is not None and (
                not self._cop_sources_known or tuple(sorted(sources)) != self._watched_cop_sources
            ):
                self.refresh()
                return
        from . import instancing

        if instancing.dependencies(self.rop) != getattr(self, "_watched_instancing", None):
            self.refresh()  # A capture referenced other prototype objects.
            return
        revision = getattr(session, "material_paths_revision", None)
        if revision is None or self.material_paths is None:
            return
        signature = (id(session), revision)
        if signature != self._material_capture_signature:
            self._material_capture_signature = signature
            # Submission-time refresh can precede the first asynchronous
            # binding scan. Subscribe after publication, without waiting for
            # conversion or scanning geometry from this host callback.
            paths = tuple(self.material_paths())
            resources = self.stylesheet_resources() if self.stylesheet_resources else ()
            style_nodes = _stylesheet_nodes(resources)
            if paths != self._watched_material_paths or style_nodes != self._watched_style_nodes:
                self.refresh(material_paths=paths)

    def _non_transform_fingerprint(self, node, names, rebuild_kinds=False):
        """(other parameter values, transform values) of an OBJ node, per parameter tuple.

        Each tuple's kind (_tuple_kind) is decided when the node's baseline is taken and kept, with the
        node's tuple names and transform split, until the next baseline, a channel edit on the node or a
        change of its parameter layout: plain values compare tuple.eval(), expressions their text
        (evaluating inside a node event would cook them), keyed tuples their channel value.
        CYCLES_HOUDINI_TUPLE_KINDS=0: the previous per-parameter rawValue fingerprint."""
        if os.environ.get("CYCLES_HOUDINI_TUPLE_KINDS") == "0":
            return self._authored_fingerprint(node, names)
        layouts = self.__dict__.setdefault("_parm_kinds", {})
        identity = node.sessionId()
        tuples = node.parmTuples()
        layout = None if rebuild_kinds else layouts.get(identity)
        if layout is not None and (layout[0] != len(tuples) or (tuples and tuples[-1].name() != layout[1])):
            layout = None  # Multiparm instances can change the tuple sequence.
        if layout is None:
            other, transform = [], []
            for index, tuple_ in enumerate(tuples):
                name = tuple_.name()
                try:
                    kind = _tuple_kind(tuple_)
                except hou.Error:
                    kind = "raw"
                if kind == "skip":
                    continue
                target = transform if (name in names or (name.startswith("i_") and name[2:] in names)) else other
                target.append((index, name, _EVALUATORS[kind]))
            layout = (len(tuples), tuples[-1].name() if tuples else None, tuple(other), tuple(transform))
            layouts[identity] = layout
        try:
            return (
                tuple([(name, evaluate(tuples[index])) for index, name, evaluate in layout[2]]),
                tuple([(name, evaluate(tuples[index])) for index, name, evaluate in layout[3]]),
            )
        except hou.Error:
            # A parameter Houdini is still writing: per-tuple, with None for the unreadable ones.
            def read(entries):
                values = []
                for index, name, evaluate in entries:
                    try:
                        values.append((name, evaluate(tuples[index])))
                    except hou.Error:
                        values.append((name, None))
                return tuple(values)

            return read(layout[2]), read(layout[3])

    def _authored_fingerprint(self, node, names):
        """The per-parameter rawValue fingerprint (CYCLES_HOUDINI_TUPLE_KINDS=0)."""
        other, transform = [], []
        for tuple_ in node.parmTuples():
            name = tuple_.name()
            target = transform if (name in names or (name.startswith("i_") and name[2:] in names)) else other
            try:
                target.append((name, tuple(_authored_value(parm) for parm in tuple_)))
            except hou.Error:
                target.append((name, None))
        return tuple(other), tuple(transform)

    def _unnamed_tuple_changes(self, node, names):
        """(names of changed non-transform tuples, transform changed) since the last look; (None, True) if unknown."""
        cache = self.__dict__.setdefault("_parm_fingerprints", {})
        try:
            identity = node.sessionId()
            current = self._non_transform_fingerprint(node, names)
        except hou.ObjectWasDeleted:
            return None, True
        previous = cache.get(identity)
        cache[identity] = current
        if previous is None:
            return None, True
        before = dict(previous[0])
        changed = tuple(name for name, value in current[0] if before.get(name, ()) != value)
        if len(previous[0]) != len(current[0]):
            changed = changed or None
        return changed, previous[1] != current[1]

    def remember_parameters(self, nodes):
        """Baseline for _unnamed_tuple_changes (called for light and OBJ sources after a read)."""
        names = ("t", "r", "s", "p", "pr", "scale", "xOrd", "rOrd")
        cache = self.__dict__.setdefault("_parm_fingerprints", {})
        sampled = set()
        for node in nodes:
            if node is None:
                continue
            try:
                identity = node.sessionId()
                if identity in sampled:
                    continue
                cache[identity] = self._non_transform_fingerprint(node, names, rebuild_kinds=True)
                sampled.add(identity)
            except hou.ObjectWasDeleted:
                pass

    def _changed(self, **kwargs):
        if self._closed:
            return
        # Node comparisons themselves raise once the ROP has been destroyed.
        # A panel can outlive scene replacement; detach all its subscriptions
        # before subsequent source deletions dispatch their notifications.
        if hou.nodeBySessionId(self._rop_identity) is None:
            self.close()
            return
        node = kwargs.get("node")
        try:
            from . import instancing

            if (
                instancing._preparing_external
                or instancing.is_generated(node)
                or instancing.is_generated(kwargs.get("child_node"))
            ):
                return
            identity = node.sessionId() if node is not None else None
            is_rop = identity == self._rop_identity
        except hou.ObjectWasDeleted:
            return
        event = kwargs.get("event_type")
        if is_rop and event == hou.nodeEventType.BeingDeleted:
            self.close()
            return
        if event == hou.nodeEventType.SpareParmTemplatesChanged:
            # A replace or rename can preserve the count and last tuple name while
            # changing what the cached positions mean. The old baseline cannot
            # classify edits to the new interface as pose or light-value edits.
            self.__dict__.setdefault("_parm_kinds", {}).pop(identity, None)
            self.__dict__.setdefault("_parm_fingerprints", {}).pop(identity, None)
            self.callback()
            return
        parm_tuple = kwargs.get("parm_tuple")
        if is_rop and parm_tuple is not None:
            name = parm_tuple.name()
            if (
                name in ("persistent_data", "picture", "crop_to_region")
                or name.startswith("output_")
                or any(
                    name.startswith("pass_" + field) and name[len("pass_" + field) :].isdigit()
                    for field in ("name", "separate", "path", "precision", "compression", "dwa_level")
                )
            ):
                # File routing and encoding do not alter the rendered buffers.
                return
        if parm_tuple is not None:
            kind = parm_tuple.parmTemplate().type()
            if kind == hou.parmTemplateType.FolderSet or (
                kind == hou.parmTemplateType.Folder and not parm_tuple.isMultiParmParent()
            ):
                # Native tabs/expansion are editor state. Multiparm counts also
                # use folder templates, but change real scene/output data and
                # must invalidate camera-only capture eligibility.
                return
            if kind == hou.parmTemplateType.Folder:
                # Edits to two multiparm counts can preserve the total but replace
                # the instances at the cached tuple positions.
                self.__dict__.setdefault("_parm_kinds", {}).pop(identity, None)
                self.__dict__.setdefault("_parm_fingerprints", {}).pop(identity, None)
            elif kind == hou.parmTemplateType.String and event == hou.nodeEventType.ParmTupleChanged:
                # Backticks can be authored in a string value without changing its template.
                self.__dict__.setdefault("_parm_kinds", {}).pop(identity, None)
        if self.track_deletions and event == hou.nodeEventType.BeingDeleted:
            try:
                if not object_scene.enabled(self.rop, hou.time()):
                    path = kwargs["node"].path()
                    for name in ("soppath", "lightpath"):
                        source = source_path(self.rop, name, hou.time())
                        if source and (source == path or source.startswith(path.rstrip("/") + "/")):
                            self.deleted_sources.add(path)
            except hou.ObjectWasDeleted:
                pass
        node = kwargs.get("node")
        if self.camera_callback is not None and node is not None:
            try:
                camera = self.rop.node(self.rop.evalParm("camera"))
                if (
                    camera is not None
                    and node not in self.non_camera_dependencies
                    and (node == camera or node in camera.inputAncestors())
                ):
                    self.camera_callback()
                    return
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
        if (self.object_pose_callback is not None or self.object_pose_source_callback is not None) and node is not None:
            try:
                names = ("t", "r", "s", "p", "pr", "scale", "xOrd", "rOrd")
                # The Instance OBJ asset exposes its transform as i_t, i_r, ...
                # Reauthoring channels/expressions changes the retained
                # dependency contract and needs an authoritative capture.
                channel_edit = event in (
                    hou.nodeEventType.ParmTupleAnimated,
                    hou.nodeEventType.ParmTupleChannelChanged,
                )
                if channel_edit:
                    # Keys or expressions added or removed: decide the tuple kinds again at the next look
                    # (a tuple whose kind changed then counts as changed: the conservative route).
                    self.__dict__.setdefault("_parm_kinds", {}).pop(identity, None)
                transform_parm = (
                    not channel_edit
                    and parm_tuple is not None
                    and (
                        parm_tuple.name() in names
                        or (parm_tuple.name().startswith("i_") and parm_tuple.name()[2:] in names)
                    )
                )
                # Houdini reports a viewport handle drag partly as ParmTupleChanged WITHOUT a tuple
                # (measured on screen: about one per three t events on a light). Classify it by what
                # actually changed on the node since the last look (CYCLES_HOUDINI_UNNAMED_TUPLE_RULE=0: off).
                if (
                    not channel_edit
                    and parm_tuple is None
                    and event == hou.nodeEventType.ParmTupleChanged
                    and node.type().category() == hou.objNodeTypeCategory()
                    and os.environ.get("CYCLES_HOUDINI_UNNAMED_TUPLE_RULE") != "0"
                ):
                    changed_other, changed_transform = self._unnamed_tuple_changes(node, names)
                    if changed_other == () and not changed_transform:
                        return  # Nothing this observer renders changed.
                    if changed_other == ():
                        transform_parm = True
                        parm_tuple = next((t for t in node.parmTuples() if t.name() in ("t", "i_t")), None)
                        transform_parm = parm_tuple is not None
                    elif (
                        changed_other
                        and self.light_value_callback is not None
                        and all(
                            node.parmTuple(name) is not None
                            and self._light_parm_edit(node, node.parmTuple(name), kwargs)
                            for name in changed_other
                        )
                        and (
                            not changed_transform
                            or self._light_parm_edit(node, node.parmTuple("t"), kwargs, transform=True)
                        )
                    ):
                        self.light_value_callback(node)  # The lights-only read includes the transform.
                        return
                if transform_parm and kwargs.get("parm_tuple") is not None:
                    # A named transform event (a handle sends t and r before its tuple-less event):
                    # take the node's baseline now if it has none (once per node, ~0.1 ms).
                    cache = self.__dict__.setdefault("_parm_fingerprints", {})
                    if identity not in cache:
                        cache[identity] = self._non_transform_fingerprint(node, names)
                # Reparenting changes the cached dependency/observer graph;
                # only an authoritative capture can accept that new contract.
                transform_event = event == hou.nodeEventType.InputDataChanged
                # A prototype's transform is baked into its instances: that needs a capture.
                prototype = node in getattr(self, "instancing_prototypes", ())
                if (
                    node.type().category() == hou.objNodeTypeCategory()
                    and (transform_parm or transform_event)
                    and not prototype
                ):
                    if (
                        transform_parm
                        and self.light_value_callback is not None
                        and self._light_parm_edit(node, parm_tuple, kwargs, transform=True)
                    ):
                        # A light's own transform: pose preview during a drag, and the
                        # read after it is lights-only (nothing else follows the light).
                        self.light_value_callback(node, pose=True)
                    elif self.object_pose_source_callback is not None:
                        self.object_pose_source_callback(node)
                    else:
                        self.object_pose_callback()
                    return
            except hou.ObjectWasDeleted:
                pass
        if self.linking_value_callback is not None and node is not None:
            try:
                if self._receiver_mask_edit(node, parm_tuple, event):
                    self.linking_value_callback(node.sessionId())
                    return
            except hou.Error:
                pass
        if self.light_value_callback is not None and node is not None and parm_tuple is not None:
            try:
                if self._light_parm_edit(node, parm_tuple, kwargs):
                    self.light_value_callback(node)
                    return
            except hou.ObjectWasDeleted:
                pass
        if self.material_value_callback is not None and node is not None:
            try:
                builder = self._material_value_edit(node, kwargs)
            except hou.Error:
                builder = None
            if builder is not None:
                self.material_value_callback(builder)
                return
        self.callback()

    def _material_value_edit(self, node, kwargs):
        """The rendered material network a value edit belongs to, or None (general notification).

        Any parameter of any node inside a rendered material network (also keyframe and expression edits)
        is a value edit of that material, except file and node references (texture and COP resources).
        InputRewired / InputDataChanged inside the network belong to the same edit while the network's
        structure equals its baseline; a structure change (wiring, bypass, new or deleted nodes) is general.
        Whether the new graph still asks the same of the geometry is decided later
        (Entry.material_value_capture). CYCLES_HOUDINI_MATERIAL_PATH=0: off.
        """
        if os.environ.get("CYCLES_HOUDINI_MATERIAL_PATH") == "0" or not self._material_wiring:
            return None
        path = node.path()
        builder = next((p for p in self._material_wiring if path == p or path.startswith(p.rstrip("/") + "/")), None)
        if builder is None:
            return None
        event = kwargs.get("event_type")
        if event in (
            hou.nodeEventType.ParmTupleChanged,
            hou.nodeEventType.ParmTupleAnimated,
            hou.nodeEventType.ParmTupleChannelChanged,
        ):
            tuple_ = kwargs.get("parm_tuple")
            if tuple_ is None:
                return None
            template = tuple_.parmTemplate()
            if template.type() == hou.parmTemplateType.String and template.stringType() in (
                hou.stringParmType.FileReference,
                hou.stringParmType.NodeReference,
                hou.stringParmType.NodeReferenceList,
            ):
                return None
            return builder
        if event in (hou.nodeEventType.InputRewired, hou.nodeEventType.InputDataChanged):
            return builder if _material_wiring(hou.node(builder)) == self._material_wiring[builder] else None
        return None

    def close(self):
        self._closed = True
        nodes, self.nodes = self.nodes, []
        for node, events in nodes:
            try:
                node.removeEventCallback(events, self._callback)
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
