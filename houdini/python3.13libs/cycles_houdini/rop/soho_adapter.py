# SPDX-License-Identifier: Apache-2.0
"""SOHO controls native IPR; the C++ adapter performs all mesh extraction."""

from __future__ import annotations

import json
import math
import os

import hou
import soho
import sohog
from .. import Session, geometry_request, matrix_from_houdini
from ..ipr import registry
from ..scene import native_scene
from . import final_render
from .rop_controls import value as rop_value


def _object_for_sop(sop):
    node = sop.parent()
    while node and node.type().category() != hou.objNodeTypeCategory():
        node = node.parent()
    if node is None:
        raise RuntimeError("The native prototype requires a SOP inside an OBJ")
    return node


def _camera(camera, now, width, height):
    # SOHO evaluates Render View/viewport overrides, including synthetic view cameras.
    values = camera.evaluate(
        {
            "world": soho.SohoParm("space:world", "float", list(hou.Matrix4(1).asTuple()), False),
            "focal": soho.SohoParm("focal", "float", [50], False),
            "aperture": soho.SohoParm("aperture", "float", [41.4214], False),
            "near": soho.SohoParm("near", "float", [0.01], False),
            "far": soho.SohoParm("far", "float", [10000], False),
            "projection": soho.SohoParm("projection", "string", ["perspective"], False),
            "orthowidth": soho.SohoParm("orthowidth", "float", [2], False),
        },
        now,
    )
    projection = values["projection"].Value[0]
    focal, aperture = values["focal"].Value[0], values["aperture"].Value[0]
    matrix = hou.Matrix4(values["space:world"].Value)
    result = dict(
        transform=matrix_from_houdini(matrix, camera=True),
        fov=2 * math.atan(aperture / (2 * focal * max(1, width / height)))
        if math.isfinite(focal) and math.isfinite(aperture) and focal > 0 and aperture > 0
        else math.nan,
        near=values["near"].Value[0],
        far=values["far"].Value[0],
        orthographic=projection in ("ortho", "orthographic"),
        projection_code=0 if projection == "perspective" else 1 if projection in ("ortho", "orthographic") else 2,
        ortho_scale=values["orthowidth"].Value[0] * height / width,
    )

    def value(name, default):
        method = camera.getDefaultedString if isinstance(default, str) else camera.getDefaultedFloat
        return method(name, now, [default])[0]

    override = value("cycles_projection", "native")
    if override != "native":
        from ..scene.camera_properties import panorama_source

        result["projection_code"] = 2 if override == "panorama" else -1
        result["orthographic"] = False
        result["panorama"] = panorama_source(value)
    return result


def _motion_camera(camera, now, width, height, motion, pixel_aspect=1):
    from .. import _native

    times = native_scene.motion_times(motion, now)
    return _native.resolve_camera_motion(
        _camera(camera, now, width, height),
        [_camera(camera, t, width, height) for t in times] if len(times) > 1 else [],
        motion["shutter_frames"],
        camera.getName(),
        motion["vectors"],
        width,
        height,
        pixel_aspect,
    )


def _camera_update(rop, camera, now, width, height):
    motion = native_scene.motion_configuration(
        rop,
        now,
        shutter_frames=camera.getDefaultedFloat("shutter", now, [0.5])[0],
        fps=soho.getDefaultedFloat("state:fps", [hou.fps()])[0],
    )
    crop = [
        camera.getDefaultedFloat(name, now, [default])[0]
        for name, default in [("cropl", 0), ("cropr", 1), ("cropb", 0), ("cropt", 1)]
    ]
    left, right = max(0, math.floor(crop[0] * width)), min(width, math.ceil(crop[1] * width))
    bottom, top = max(0, math.floor(crop[2] * height)), min(height, math.ceil(crop[3] * height))
    from ..scene import camera_properties

    near_clip = camera.getDefaultedFloat("near", now, [0.01])[0]

    def lens_sample(t):
        def lens_value(name, default):
            method = camera.getDefaultedString if isinstance(default, str) else camera.getDefaultedFloat
            return method(name, t, [default])[0]

        return camera_properties.evaluate(
            lens_value,
            rop_value(rop, "dof_mode", now),
            soho.getDefaultedFloat("state:houdiniunitlength", [1.0])[0],
            near_clip,
        )

    times = native_scene.motion_times(motion, now) if motion["enabled"] else [now]
    dof = camera_properties.resolve_lens(lens_sample, now, times, camera.getName(), near_clip)

    def sampling_sample(t):
        def value(name, default):
            method = camera.getDefaultedString if isinstance(default, str) else camera.getDefaultedFloat
            return method(name, t, [default])[0]

        node = hou.node(camera.getName())
        return camera_properties.sampling_source(
            value,
            lambda: node.parm("cycles_shutter_curve").evalAtTime(t)
            if node and node.parm("cycles_shutter_curve")
            else None,
        )

    sampling = (
        camera_properties.resolve_sampling(sampling_sample, now, times, camera.getName())
        if motion["enabled"]
        else dict(rolling_shutter=False, rolling_duration=0.1, shutter_curve=[], source_diagnostics=[])
    )
    from . import output

    region = (
        output.crop(rop, now, width, height)
        if output.value(rop, "render_region", now)
        else (left, bottom, right - left, top - bottom)
    )
    return dict(
        camera=_motion_camera(camera, now, width, height, motion, rop.parm("pixel_aspect").evalAtTime(now)),
        camera_dof=dof,
        camera_sampling=sampling,
        motion=motion,
        pixel_aspect=rop.parm("pixel_aspect").evalAtTime(now),
        crop=region,
    )


def _reuse_native_update(entry, config, now, camera_update, mode):
    # SOHO also invokes updates for final-only/UI parameters. Host notifications,
    # SOHO dirty objects, time/configuration and camera overrides must ALL be
    # unchanged before avoiding extraction. No source hashes or SOP reads here.
    if entry is None:
        return False, dict(existing=False)
    status = entry.session.status()
    checks = dict(
        update=mode == "update",
        bound=bool(entry.channel),
        observed=entry.observer is not None,
        healthy=not entry.error,
        configuration=entry.config == config,
        time=entry.observed_time == now,
        notifications=entry.change_serial == entry.applied_serial,
        active=status["state"] not in ("idle", "closed", "failed", "cancelled"),
        color=not status.get("color_update_pending"),
        clean_objects=not any(soho.objectList("objlist:dirtyinstance")),
        no_deletions=not any(soho.objectList("objlist:deletedinstance")),
        camera=entry.last_soho.get("camera_update") == camera_update,
    )
    return all(checks.values()), checks


def _update(rop, camera, now, width, height, mode, camera_update=None):
    from .passes import configuration as pass_configuration, custom_configuration

    camera_update = _camera_update(rop, camera, now, width, height) if camera_update is None else camera_update
    motion = camera_update["motion"]
    entry = registry.get(f"rop:{rop.sessionId()}")
    deleted_sources = entry.observer.deleted_sources if entry and entry.observer else ()
    camera_only = bool(
        mode == "update"
        and entry
        and entry.can_capture_camera_only(motion, now)
        and not any(soho.objectList("objlist:dirtyinstance"))
        and not any(soho.objectList("objlist:deletedinstance"))
    )
    if camera_only:
        return dict(camera_update)
    if (
        mode == "update"
        and entry
        and not any(soho.objectList("objlist:dirtyinstance"))
        and not any(soho.objectList("objlist:deletedinstance"))
    ):
        # Narrow updates (Entry.narrow_update, shared with the Cycles Render panel).
        narrow = entry.narrow_update(now, motion, camera_update)
        if narrow is not None:
            return narrow
    sources = native_scene.scene_lighting_configuration(
        rop, now, motion, deleted_sources, interactive=mode in ("generate", "update")
    )
    return dict(
        **sources,
        **camera_update,
        dicing=native_scene.dicing_configuration(
            rop, now, render_camera_path=camera.getName(), interactive=mode in ("generate", "update")
        ),
        passes=pass_configuration(rop, now),
        **custom_configuration(rop, now),
        denoising=native_scene.denoising_configuration(rop, now, interactive=mode in ("generate", "update")),
        rendering=native_scene.rendering_configuration(rop, now, interactive=mode in ("generate", "update")),
        **native_scene.film_transparency(rop, now),
        background=rop.parmTuple("background").evalAtTime(now),
        **native_scene.material_configuration(rop, now, motion),
    )


def render():
    rop = None
    mode = None
    try:
        now = soho.getDefaultedFloat("state:time", [hou.time()])[0]
        mode = soho.getDefaultedString("state:previewmode", ["default"])[0]
        driver = soho.getOutputDriver()
        rop = hou.node(driver.getName())
        if rop is None:
            raise RuntimeError("Missing Cycles output driver")
        camera_path = soho.getDefaultedString("camera", ["/obj/cam1"])[0]
        registry.trace("soho_enter", camera=camera_path, now=now)
        if not soho.initialize(now, camera_path, {"state:autoheadlight": False}):
            raise RuntimeError("Could not initialize the SOHO camera")
        object_path, light_path = native_scene.tracking_paths(rop, now)
        # Native selection is authoritative, including Force Objects. SOHO
        # provides native-view dependencies/camera overrides for those paths.
        soho.addObjects(now, object_path, light_path, "", True)
        soho.lockObjects(now)
        # SOHO must see geometry access to register SOP dependencies for native IPR.
        # Merely cooking through HDK does not register that dependency with the viewer.
        for instance in soho.objectList("objlist:instance"):
            instance.getDefaultedFloat("space:world", now, list(hou.Matrix4(1).asTuple()))
            path = instance.getDefaultedString("object:soppath", now, [""])[0]
            if path:
                tracked_geometry = sohog.SohoGeometry(path, now)
                # Failed source cooks are diagnosed and omitted by the shared
                # native capture path. Tracking must not reject other objects.
        # HOM evaluation alone does not establish SOHO's transform dependencies.
        for light in soho.objectList("objlist:light"):
            light.getDefaultedFloat("space:world", now, list(hou.Matrix4(1).asTuple()))
        cameras = list(soho.objectList("objlist:camera"))
        if not cameras:
            raise RuntimeError("No active SOHO camera")
        camera = cameras[0]
        registry.trace("soho_mode", mode=mode, camera=camera.getName(), driver=driver.getName())
        config = native_scene.configuration(rop, at_time=now, interactive=mode in ("generate", "update"))
        camera_update = _camera_update(rop, camera, now, config[1], config[2])
        existing = registry.get(f"rop:{rop.sessionId()}")
        reuse, reuse_checks = _reuse_native_update(existing, config, now, camera_update, mode)
        if reuse:
            generation = existing.session.status()["generation"]
            existing.last_soho.update(mode=mode, generation=generation, reused=True, reuse_checks=reuse_checks)
            registry.trace("native_unchanged", generation=generation)
            print(
                json.dumps(dict(protocol=1, event="update", channel=existing.channel, generation=generation)),
                flush=True,
            )
            return
        if mode == "update" and all(reuse_checks.get(name) for name in ("configuration", "time", "camera")):
            # Mid-gesture light/object transforms: pose preview, capture after release.
            generation = existing.gesture_pose_update()
            if generation is not None:
                existing.last_soho.update(mode=mode, generation=generation, reused=True, pose_gesture=True)
                registry.trace("native_pose_gesture", generation=generation)
                print(
                    json.dumps(dict(protocol=1, event="update", channel=existing.channel, generation=generation)),
                    flush=True,
                )
                return
        update = _update(rop, camera, now, config[1], config[2], mode, camera_update)
        if mode in ("generate", "update"):
            entry = registry.for_rop(rop, config)
            joining_panel = not entry.channel and entry.views > 0
            if mode == "generate" or not entry.channel:
                socket_port = camera.getDefaultedString("vm_image_mplay_socketport", now, [""])[0]
                if not socket_port:
                    raise RuntimeError("Native IPR did not provide an image socket")
                message = registry.bind_native(entry, socket_port)
                registry.trace("native_bound", socket_port=socket_port)
                print(json.dumps(message), flush=True)
            # A repeated update can complete an earlier pose preview without
            # resetting identical engine work. Generate still forces a restart.
            generation = entry.apply(
                update,
                now,
                reuse_if_unchanged=joining_panel or mode == "update",
                change_serial=entry.change_serial if "geometry" not in update else None,
            )
            entry.last_soho = dict(
                mode=mode,
                host_pid=os.getpid(),
                camera=camera.getName(),
                generation=generation,
                camera_update=camera_update,
                reused=False,
                reuse_checks=reuse_checks,
                dirty=[o.getName() for o in soho.objectList("objlist:dirtyinstance")],
                deleted=[o.getName() for o in soho.objectList("objlist:deletedinstance")],
            )
            print(
                json.dumps(dict(protocol=1, event="update", channel=entry.channel, generation=generation)), flush=True
            )
            return
        final_render.render_frame(rop, config, update, now)
    except Exception as error:
        registry.trace("soho_failure", error=str(error))
        if rop is not None:
            if mode in ("generate", "update"):
                entry = registry.get(f"rop:{rop.sessionId()}")
                if entry is not None:
                    entry.fail(error)
            else:
                final_render.finish(rop, reason="failed", error=error)
        message = str(error)
        if isinstance(error, final_render.RenderGPUFailure):
            if hou.isUIAvailable():
                message = error.info["message"]
            else:
                first_error = error.info.get("first_error")
                if first_error and first_error not in message:
                    message = f"{first_error}\n{message}"
        soho.error(message)
