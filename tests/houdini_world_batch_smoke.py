# SPDX-License-Identifier: Apache-2.0
"""Five actual ROP frames: animated HDRI, moving geometry and retained resources."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
from cycles_houdini import final_render, light_properties, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage3/world/batch"
destination.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
scene = hou.node("/obj").createNode("subnet", "world_sequence")
receiver = scene.createNode("geo", "static_receiver", run_init_scripts=False)
grid = receiver.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((2, 2))
grid.setRenderFlag(True)
moving = scene.createNode("geo", "deforming_mesh", run_init_scripts=False)
moving.parm("tx").set(-1.4)
box = moving.createNode("box")
box.parmTuple("size").set((0.2, 0.7, 0.2))
box.parm("sizez").setExpression(".2 + .02 * ($F - 1)", hou.exprLanguage.Hscript)
box.setRenderFlag(True)
environment = scene.createNode("envlight")
light_properties.install(environment)
environment.setParms(dict(light_contribprimary=1, cycles_environment_colorspace="data"))
environment.parm("env_map").set(str(destination / "world.$F4.exr"))
for number in range(1, 6):
    color = np.array((0.125, 0.25, 0.5), np.float32) * number
    spec = OIIO.ImageSpec(128, 64, 3, OIIO.FLOAT)
    spec.attribute("oiio:ColorSpace", "data")
    image = OIIO.ImageOutput.create(str(destination / f"world.{number:04d}.exr"))
    assert image.open(str(destination / f"world.{number:04d}.exr"), spec)
    assert image.write_image(np.broadcast_to(color, (64, 128, 3)).copy()) and image.close()
camera = hou.node("/obj").createNode("cam", "world_sequence_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "world_sequence_rop")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        camera=camera.path(),
        world_source="scene",
        vobject="*",
        lights="*",
        resx=128,
        resy=96,
        samples=128,
        material_kind="diffuse",
        use_material_assignments=0,
        roughness=0,
        persistent_data=1,
        use_denoise=0,
        picture=str(destination / "render.$F4.exr"),
    )
)
rop.parmTuple("basecolor").set((1, 1, 1))
factory = registry.session_from_configuration
original_wait = final_render.wait_for_completion
created = []
frames = []
images = []


def record_factory(*args, **kwargs):
    session = factory(*args, **dict(kwargs, collect_diagnostics=True))
    created.append(session)
    return session


def record_wait(session, generation, **kwargs):
    result = original_wait(session, generation, **kwargs)
    frames.append(dict(status=session.status(), diagnostics=session.diagnostics(), textures=session.texture_inputs()))
    return result


registry.session_from_configuration = record_factory
final_render.wait_for_completion = record_wait
try:
    final_render.register(rop)
    rop.render(frame_range=(1, 5, 1))
    assert len(created) == 1 and len(frames) == 5 and not final_render.active_sequences()
    assert created[0].status()["state"] == "closed" and created[0].status()["device_allocation_bytes"] == 0
    assert [frame["status"]["geometry_conversions"] for frame in frames] == [2, 3, 4, 5, 6], frames
    assert [frame["diagnostics"]["background_builds"] for frame in frames] == [1, 2, 3, 4, 5], frames
    if "renderer_pid" in frames[0]["status"]:
        assert len({frame["status"]["renderer_pid"] for frame in frames}) == 1
    for number, frame in enumerate(frames, 1):
        assert len(frame["textures"]) == 1 and frame["textures"][0]["material_key"] == "World"
        assert Path(frame["textures"][0]["filename"]) == destination / f"world.{number:04d}.exr"
        path = destination / f"render.{number:04d}.exr"
        image = OIIO.ImageInput.open(str(path))
        assert image, OIIO.geterror()
        spec = image.spec()
        pixels = image.read_image(format=OIIO.FLOAT)
        image.close()
        rgba = pixels[:, :, [spec.channelnames.index(channel) for channel in ("R", "G", "B", "A")]]
        assert np.isfinite(rgba).all()
        expected = np.array((0.125, 0.25, 0.5)) * number
        # Exact world radiance and independently specified Lambertian unit
        # reflectance; noisy surface estimates are averaged before comparison.
        background = rgba[2, 2, :3]
        diffuse = rgba[42:54, 58:70, :3].mean((0, 1))
        assert np.allclose(background, expected, atol=0.001, rtol=0), (number, background, expected)
        assert np.allclose(diffuse, expected, atol=0.025, rtol=0), (number, diffuse, expected)
        images.append(
            dict(
                frame=number,
                path=str(path),
                background=background.tolist(),
                diffuse=diffuse.tolist(),
                expected=expected.tolist(),
            )
        )
    assert hou.frame() == 1
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"]
    (destination / "houdini-world-batch-smoke.json").write_text(
        json.dumps(
            dict(
                result="passed",
                device=device,
                execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"),
                images=images,
                frames=frames,
                summary=summary,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(dict(result="passed", frames=5)))
finally:
    registry.session_from_configuration = factory
    final_render.wait_for_completion = original_wait
    final_render.close_all()
    registry.close_all()
