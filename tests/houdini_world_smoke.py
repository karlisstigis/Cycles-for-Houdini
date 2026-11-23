# SPDX-License-Identifier: Apache-2.0
"""Independent world radiance, direction, input color and resource contracts."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO

import cycles_houdini as ch
from cycles_houdini import native_scene, registry, menu_actions, light_properties
from cycles_houdini.materials import create_world_material, material_graph, WORLD_BUILDER

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = Path(os.environ.get("CYCLES_HOUDINI_TEST_OUTPUT_ROOT", artifacts_root() / "stage3/world"))
destination.mkdir(parents=True, exist_ok=True)
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
checks = []
original_ocio = hou.getenv("OCIO")
original_config = OCIO.GetCurrentConfig()


def write(path, pixels, colorspace="data"):
    spec = OIIO.ImageSpec(pixels.shape[1], pixels.shape[0], 3, OIIO.FLOAT)
    spec.attribute("oiio:ColorSpace", colorspace)
    output = OIIO.ImageOutput.create(str(path))
    assert output.open(str(path), spec) and output.write_image(pixels) and output.close()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


def center(frame):
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(64, 96, 4)
    assert np.isfinite(image).all()
    return image[31:33, 47:49, :3].mean(axis=(0, 1))


def close(a, b, tolerance=2e-4):
    assert np.max(np.abs(np.asarray(a) - np.asarray(b))) <= tolerance, (list(a), list(b))


scene = hou.node("/obj").createNode("subnet", "world_test")
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=2))
rop = menu_actions.create_rop()
set_rop_devices(rop, device)
rop.setParms(
    dict(
        objectroot=scene.path(),
        camera=camera.path(),
        vobject="",
        lights="*",
        resx=96,
        resy=64,
        samples=128,
        world_source="color",
        use_denoise=0,
        film_exposure=1,
    )
)
rop.parmTuple("background").set((0.125, 0.25, 0.5))
entry = registry.for_rop(rop, native_scene.configuration(rop))
notifications = []
observer = native_scene.SceneObserver(rop, lambda: notifications.append(True))
for folders, index in zip(
    rop.parm("world_source").containingFolderSetParmTuples(), rop.parm("world_source").containingFolderIndices()
):
    folders[0].set(index)
assert not notifications, "Native folder navigation notified a render scene edit"
rop.parm("world_sampling").set("manual")
assert notifications
rop.parm("world_sampling").set("auto")
observer.close()


def render(now=None):
    now = hou.time() if now is None else now
    return entry.session.wait(entry.apply(native_scene.capture(rop, now), now))


def record(name, **details):
    checks.append(dict(name=name, status=entry.session.status(), **details))


def texture_changed():
    deadline = time.monotonic() + 10
    while not entry.session.textures_changed():
        if time.monotonic() > deadline:
            raise TimeoutError("World image revision was not detected")
        time.sleep(0.02)


try:
    first = render()
    close(center(first), (0.125, 0.25, 0.5))
    record("Legacy constant world remains numerically unchanged")
    builder = create_world_material(name="world_nodes")
    assert builder.type().name() == WORLD_BUILDER and builder.node("world_output")
    background = builder.node("background_shader")
    background.parmTuple("color").set((0.25, 0.125, 0.0625))
    background.parm("strength").set(2)
    rop.setParms(dict(world_source="material", world_material=builder.path()))
    world_frame = render()
    close(center(world_frame), (0.5, 0.25, 0.125))
    builds = entry.session.diagnostics()["background_builds"]
    camera.parm("tx").set(0.5)
    render()
    assert entry.session.diagnostics()["background_builds"] == builds
    rop.parm("world_sampling").set("none")
    render()
    assert entry.session.diagnostics()["background_builds"] == builds
    record("Native World Material is rendered; camera and sampling changes retain its graph")
    rop.parm("world_visibility_camera").set(0)
    close(center(render()), (0, 0, 0))
    rop.parm("world_visibility_camera").set(1)
    rop.parm("transparent_background").set(1)
    transparent = render()
    pixels = np.frombuffer(transparent["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
    assert np.max(np.abs(pixels[:, :, 3])) == 0
    rop.parm("transparent_background").set(0)
    rop.parm("world_sampling").set("auto")
    rop.parm("world_source").set("color")
    close(center(render()), (0.125, 0.25, 0.5))
    record("World visibility and transparent film work; switching back to constant color clears the graph")

    environment = scene.createNode("envlight", "environment")
    assert light_properties.install(environment) and not light_properties.install(environment)
    assert environment.parm("cycles_environment_colorspace") and not environment.parm("cycles_light_radius")
    assert environment.parm("ry").containingFolders()[0] == "Transform"
    environment.setParms(
        dict(light_contribprimary=1, light_intensity=2, light_exposure=1, cycles_environment_colorspace="data")
    )
    environment.parmTuple("light_color").set((0.25, 0.125, 0.0625))
    rop.parm("world_source").set("scene")
    close(center(render()), (1.0, 0.5, 0.25))
    # Lambertian unit reflectance under a uniform world independently gives L.
    geo = scene.createNode("geo", "receiver")
    grid = geo.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((8, 8))
    grid.setRenderFlag(True)
    grid.setDisplayFlag(True)
    rop.setParms(dict(vobject="receiver", material_kind="diffuse", use_material_assignments=0))
    rop.parmTuple("basecolor").set((1, 1, 1))
    rop.parm("roughness").set(0)
    close(center(render()), (1.0, 0.5, 0.25), 0.025)
    conversions = entry.session.status()["geometry_conversions"]
    environment.parm("light_contribprimary").set(0)
    close(center(render()), (1.0, 0.5, 0.25), 0.025)
    assert entry.session.status()["geometry_conversions"] == conversions
    record(
        "Native environment radiance and Lambertian illumination follow color, power and exposure; camera visibility does not hide illumination"
    )

    rop.parm("vobject").set("")
    environment.setParms(dict(light_contribprimary=1, light_intensity=1, light_exposure=0))
    environment.parmTuple("light_color").set((1, 1, 1))
    # File rows explicitly label the native Y-up poles and four longitude bands.
    pano = np.empty((64, 128, 3), np.float32)
    labels = np.asarray(((0.125, 0.25, 0.5), (0.5, 0.125, 0.25), (0.25, 0.5, 0.125), (0.5, 0.25, 0.125)), np.float32)
    for band in range(4):
        pano[:, band * 32 : (band + 1) * 32] = labels[band]
    pano[:12] = (0.75, 0.125, 0.0625)
    pano[-12:] = (0.0625, 0.125, 0.75)
    path = destination / "direction-labeled.exr"
    write(path, pano)
    environment.parm("env_map").set(str(path))
    # Center directions are kept inside each labeled region, away from seams.
    camera.parm("projection").set(0)
    camera.parm("focal").set(200)
    # Avoid the exact polar singularity: upstream linear Repeat filtering wraps
    # the first/last image rows within half a texel of v=0/1. These directions
    # lie inside independently labeled polar caps and test the same orientation.
    for name, rotation, expected in (
        ("north", (80, 0, 0), (0.75, 0.125, 0.0625)),
        ("south", (-80, 0, 0), (0.0625, 0.125, 0.75)),
    ):
        camera.parmTuple("r").set(rotation)
        close(center(render()), expected)
    camera.parmTuple("r").set((0, 20, 0))
    environment.parm("ry").set(0)
    north_world = center(render())
    close(north_world, labels[1])
    environment.parm("ry").set(90)
    rotated_world = center(render())
    close(rotated_world, labels[2])
    environment.parm("ry").set(0)
    close(center(render()), north_world)
    assert entry.session.status()["geometry_conversions"] == conversions
    record("The labeled image verifies native Y-up poles, environment rotation, restoration and retained geometry")

    # Constant texels isolate file refresh and color management from projection.
    constant = np.broadcast_to(np.array((0.25, 0.5, 1), np.float32), (64, 128, 3)).copy()
    write(path, constant)
    texture_changed()
    close(center(render()), (0.25, 0.5, 1))
    inputs = entry.session.texture_inputs()
    assert len(inputs) == 1 and inputs[0]["material_key"] == "World"
    first_revision = inputs[0]["revision"]
    write(path, constant * 2)
    texture_changed()
    close(center(render()), (0.5, 1, 2))
    assert entry.session.texture_inputs()[0]["revision"] != first_revision
    record("Disk-only environment edits replace texture content with owned revisions")

    missing = destination / ("uncreated-" + uuid.uuid4().hex + ".exr")
    environment.parm("env_map").set(str(missing))
    close(center(render()), (0.125, 0.25, 0.5))
    assert not entry.session.status()["error"]
    assert any(issue["code"] == "world.unavailable" for issue in entry.session.status()["source_diagnostics"])
    write(missing, constant)
    close(center(render()), (0.25, 0.5, 1))
    assert not any(issue["code"] == "world.unavailable" for issue in entry.session.status()["source_diagnostics"])
    environment.parm("env_map").set(str(path))
    record("Missing-image recovery restores rendering")

    second = scene.createNode("envlight", "second_environment")
    close(center(render()), (0.125, 0.25, 0.5))
    assert any(
        issue["code"] == "world.unavailable" and "one world" in issue["message"]
        for issue in entry.session.status()["source_diagnostics"]
    )
    second.parm("light_enable").set(0)
    render()
    second.destroy()
    environment.parm("env_portalenable").set(1)
    close(center(render()), (0.5, 1, 2))
    assert any(
        issue["code"] == "world.unsupported_setting" and "portal" in issue["message"]
        for issue in entry.session.status()["source_diagnostics"]
    )
    environment.parm("env_portalenable").set(0)
    environment.parm("env_map").set("")
    environment.parm("env_clipy").set(1)
    camera.parmTuple("r").set((90, 0, 0))
    close(center(render()), (1, 1, 1))
    camera.parmTuple("r").set((-90, 0, 0))
    close(center(render()), (0, 0, 0))
    record("Native hemisphere clipping is rendered; unsupported and ambiguous native settings are diagnosed")

    environment.parm("light_enable").set(0)
    close(center(render()), (0.125, 0.25, 0.5))
    rop.setParms(dict(world_source="material", world_material=builder.path()))
    image = builder.createNode("cycles::environment_texture::1", "hdr_image")
    image.setParms(dict(filename=str(path), colorspace="data"))
    background.setNamedInput("color", image, "color")
    background.parm("strength").set(1)
    close(center(render()), (0.5, 1, 2))
    promote = ch.promote_parameter(builder, background, "strength", name="world_strength")
    promote.set((3,))
    close(center(render()), (1.5, 3, 6))
    saved = destination / "world-material-native.hiplc"
    rop_path = rop.path()
    builder_path = builder.path()
    hou.hipFile.save(str(saved))
    registry.close_all()
    hou.hipFile.load(str(saved), suppress_save_prompt=True)
    rop = hou.node(rop_path)
    builder = hou.node(builder_path)
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    close(center(render()), (1.5, 3, 6))
    record("World texture nodes, promoted controls and HIP reload preserve rendered behavior")
    image = hou.node(builder.path() + "/hdr_image")
    image.parm("colorspace").set("auto")
    background = image.parent().node("background_shader")
    # Independent OCIO processors define the expected HDRI radiance. Neither
    # expected values nor input space names come from the plugin's conversion.
    rec709 = "Linear Rec.709 (sRGB)"
    write(path, constant, colorspace=rec709)
    input_results = []
    for working in (rec709, "ACEScg"):
        config = OCIO.Config.CreateFromStream(original_config.serialize())
        config.setWorkingDir(original_config.getWorkingDir())
        config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
        config.validate()
        profile = destination / ("world-" + working.replace(" ", "_").replace("/", "_") + ".ocio")
        profile.write_text(config.serialize(), encoding="utf-8")
        hou.putenv("OCIO", str(profile))
        hou.Color.reloadOCIO()
        ch.synchronize_color_configuration()
        expected = (
            np.asarray(config.getProcessor(rec709, working).getDefaultCPUProcessor().applyRGB([0.25, 0.5, 1])) * 3
        )
        measured = center(render())
        close(measured, expected, 0.001)
        resolved = entry.session.texture_inputs()[0]
        assert resolved["colorspace"] == rec709, resolved
        input_results.append(
            dict(working=working, expected=expected.tolist(), measured=measured.tolist(), input=resolved)
        )
    image.parm("colorspace").set("data")
    close(center(render()), (0.75, 1.5, 3))
    record(
        "World Auto input and Rec.709/ACEScg working spaces match independent OCIO processors; explicit Data bypasses conversion",
        profiles=input_results,
    )
    # A SOP-source ROP must track worlds independently of OBJ geometry traversal.
    network = hou.node(rop.evalParm("objectroot"))
    rop.setParms(dict(source_mode="sop", soppath=network.node("receiver").renderNode().path(), world_source="scene"))
    notifications = []
    observer = native_scene.SceneObserver(rop, lambda: notifications.append(True))
    new_environment = network.createNode("envlight", "sop_environment")
    assert notifications, "SOP mode missed environment creation"
    observer.refresh()
    notifications.clear()
    new_environment.parm("light_intensity").set(2)
    assert notifications
    render()
    notifications.clear()
    new_environment.parm("light_enable").set(0)
    assert notifications
    observer.refresh()
    notifications.clear()
    new_environment.parm("light_enable").set(1)
    assert notifications, "SOP mode did not observe a disabled environment"
    observer.refresh()
    notifications.clear()
    new_environment.destroy()
    assert notifications, "SOP mode missed environment deletion"
    observer.close()
    render()
    record("Explicit SOP rendering observes environment creation, parameters, disable/enable and deletion")
    rig = network.createNode("subnet", "environment_rig")
    nested_environment = rig.createNode("envlight", "nested_environment")
    nested_environment.parm("env_map").set(str(path))
    notifications = []
    observer = native_scene.SceneObserver(rop, lambda: notifications.append(True))
    before_world = native_scene.capture(rop)["world"]
    rig.parm("ry").set(30)
    assert notifications, "SOP mode missed an environment parent transform"
    assert native_scene.capture(rop)["world"] != before_world
    observer.close()
    render()
    record("Explicit SOP rendering observes nested environment parent transforms")
    report = dict(
        result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
    )
    (destination / "houdini-world-smoke.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(result="passed", checks=len(checks))))
finally:
    registry.close_all()
    if original_ocio is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_ocio)
    hou.Color.reloadOCIO()
