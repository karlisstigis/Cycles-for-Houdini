# SPDX-License-Identifier: Apache-2.0
"""World importance-map dimensions, independent lighting and retained meshes.

Copy counters describe logical device requests, not physical PCIe transfers.
Timing is diagnostic; this small fixture is not an IPR performance benchmark.
"""

from support.paths import artifacts_root

import argparse
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import light_properties, native_scene

device = argparse.ArgumentParser()
device.add_argument("--device", default="CPU")
device = device.parse_args().device
destination = artifacts_root() / "stage3/world/sampling"
destination.mkdir(parents=True, exist_ok=True)
color = np.array((0.25, 0.5, 1), np.float32)
checks = []
report = dict(
    result="running", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
)


def write(path, width, height):
    spec = OIIO.ImageSpec(width, height, 3, OIIO.FLOAT)
    spec.attribute("oiio:ColorSpace", "data")
    image = OIIO.ImageOutput.create(str(path))
    assert image.open(str(path), spec)
    assert image.write_image(np.broadcast_to(color, (height, width, 3)).copy()) and image.close()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


scene = hou.node("/obj").createNode("subnet", "world_sampling")
camera = scene.createNode("cam")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
receiver = scene.createNode("geo", run_init_scripts=False)
grid = receiver.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((2, 2))
grid.setRenderFlag(True)
environment = scene.createNode("envlight")
light_properties.install(environment)
path = destination / "small.exr"
larger = destination / "larger.exr"
write(path, 128, 64)
write(larger, 512, 256)
environment.setParms(dict(env_map=str(path), light_contribprimary=1, cycles_environment_colorspace="data"))
rop = hou.node("/out").createNode("cycles_ipr")
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        vobject="*",
        lights="*",
        camera=camera.path(),
        resx=96,
        resy=64,
        samples=128,
        world_source="scene",
        material_kind="diffuse",
        roughness=0,
        use_material_assignments=0,
    )
)
rop.parmTuple("basecolor").set((1, 1, 1))


def node(name, definition, **parameters):
    return dict(id=name, definition="cycles/" + definition + "/1", parameters=parameters)


def link(source, socket, destination, input):
    return {"from": dict(node=source, socket=socket), "to": dict(node=destination, socket=input)}


def check(session, label, update, resolution, uniform=True, geometry_retained=True):
    started = time.perf_counter()
    frame = session.wait(session.apply(update, hou.time()))
    diagnostic = session.diagnostics()
    status = session.status()
    assert diagnostic["enabled"] and not diagnostic["engine_statistics_pending"]
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
    measured = dict(background=pixels[2, 2, :3].tolist(), diffuse=pixels[27:37, 42:54, :3].mean((0, 1)).tolist())
    checks.append(
        dict(
            name=label,
            seconds=time.perf_counter() - started,
            resolution=resolution,
            measured=measured,
            status=status,
            diagnostics=diagnostic,
        )
    )
    assert np.isfinite(pixels).all()
    if uniform:
        assert np.allclose(measured["background"], color, rtol=0, atol=0.001), measured
        assert np.allclose(measured["diffuse"], color, rtol=0, atol=0.025), measured
    else:
        assert np.ptp(pixels[:10, :, :3]) > 0.001
    copies = diagnostic["buffer_copy_requests"]
    if resolution is None:
        assert "light_background_conditional_cdf" not in copies and "light_background_marginal_cdf" not in copies
    else:
        width, height = resolution
        # One float2 per conditional entry and marginal row, including the
        # terminal CDF entries. The pinned ShaderEval input is eight 32-bit
        # words since patches 0024/0025 added time, shader and alignment padding.
        assert copies["light_background_conditional_cdf"]["requested_bytes"] == (width + 1) * height * 8, copies
        assert copies["light_background_marginal_cdf"]["requested_bytes"] == (height + 1) * 8, copies
        assert copies["ShaderEval input"]["requested_bytes"] == width * height * 32, copies
    if geometry_retained:
        assert status["geometry_conversions"] == 1
        assert not any(name in copies for name in ("tri_vindex", "tri_verts", "tri_shader"))
    return diagnostic


try:
    with ch.Session(device=device, width=96, height=64, samples=128, collect_diagnostics=True) as session:
        check(
            session, "Native Auto uses HDRI dimensions", native_scene.capture(rop), (128, 64), geometry_retained=False
        )
        builds = session.diagnostics()["background_builds"]
        environment.parm("cycles_environment_map_resolution").set(256)
        check(session, "Native manual resolution remains explicit", native_scene.capture(rop), (256, 128))
        assert session.diagnostics()["background_builds"] == builds
        environment.parm("cycles_light_use_mis").set(0)
        check(session, "MIS disabled avoids importance-map work", native_scene.capture(rop), None)
        environment.setParms(dict(cycles_light_use_mis=1, cycles_environment_map_resolution=0))
        write(path, 512, 256)
        check(session, "A same-path image resize updates Auto dimensions", native_scene.capture(rop), (512, 256))
        graph = dict(
            schema_version=1,
            nodes=[
                node("small", "environment_texture", filename=str(path), colorspace="data"),
                node("large", "environment_texture", filename=str(larger), colorspace="data"),
                node("add", "vector_math", math_type="add"),
                node("background", "background_shader", strength=0.5),
            ],
            links=[
                link("small", "color", "add", "vector1"),
                link("large", "color", "add", "vector2"),
                link("add", "vector", "background", "color"),
            ],
            surface=dict(node="background", socket="background"),
        )
        write(path, 128, 64)
        check(session, "A mixed World Material uses the largest environment", dict(world=dict(graph=graph)), (512, 256))
        before = session.diagnostics()["background_builds"]
        camera.parm("tx").set(0.05)
        check(session, "Camera-only changes retain the importance map", dict(camera_path=camera.path()), None)
        assert session.diagnostics()["background_builds"] == before
        procedural = dict(
            schema_version=1,
            nodes=[
                node("direction", "geometry"),
                node("noise", "noise_texture"),
                node("background", "background_shader"),
            ],
            links=[link("direction", "position", "noise", "vector"), link("noise", "color", "background", "color")],
            surface=dict(node="background", socket="background"),
        )
        check(
            session,
            "Procedural worlds retain the documented fallback",
            dict(world=dict(graph=procedural)),
            (1024, 512),
            uniform=False,
        )
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "houdini-world-sampling-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
