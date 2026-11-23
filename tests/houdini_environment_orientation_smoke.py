# SPDX-License-Identifier: Apache-2.0
"""Native envlight longitude matches a calibrated Houdini viewport panorama."""

from support.paths import artifacts_root

import argparse, json, math, os
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, light_properties
from cycles_houdini.device_selection import set_devices
from cycles_houdini.materials import create_world_material

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path)
args = p.parse_args()
dest = Path(
    os.environ.get(
        "CYCLES_HOUDINI_TEST_OUTPUT_ROOT",
        artifacts_root() / "stage3/world/orientation",
    )
)
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
path = dest / "longitude.exr"
labels = np.asarray(((1, 0.01, 0.01), (0.01, 1, 0.01), (0.01, 0.01, 1), (1, 1, 0.01)), np.float32)
image = np.tile(np.repeat(labels, 64, axis=0)[None, :, :], (128, 1, 1))
image[:16] = (0.7, 0.1, 0.7)
image[-16:] = (0.1, 0.7, 0.7)
out = oiio.ImageOutput.create(str(path))
spec = oiio.ImageSpec(256, 128, 3, oiio.FLOAT)
spec.attribute("oiio:ColorSpace", "data")
assert out.open(str(path), spec) and out.write_image(image) and out.close()
rig = hou.node("/obj").createNode("subnet", "environment_orientation")
env = rig.createNode("envlight", "environment")
light_properties.install(env)
env.setParms(dict(env_map=str(path), light_contribprimary=1, cycles_environment_colorspace="data"))
cam = hou.node("/obj").createNode("cam", "environment_orientation_camera")
cam.parm("focal").set(1000)
rop = hou.node("/out").createNode("cycles_ipr", "environment_orientation")
set_devices(rop, args.device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject="",
        lights=env.path(),
        camera=cam.path(),
        world_source="scene",
        resx=32,
        resy=32,
        samples=8,
        ipr_samples=8,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
    )
)
entry = registry.for_rop(rop, native_scene.configuration(rop))
worker = None


def expected():
    # Independently specified native panorama convention: -Z is u=.5, +Y
    # is the upper pole. Inverse object rotation also covers rotated parents.
    ray = hou.Vector3(0, 0, -1) * hou.Matrix3(cam.worldTransform())
    local = (ray * hou.Matrix3(env.worldTransform()).inverted()).normalized()
    u = (0.5 + math.atan2(local[0], -local[2]) / (2 * math.pi)) % 1
    v = math.acos(max(-1, min(1, local[1]))) / math.pi
    return image[min(127, int(v * 128)), min(255, int(u * 256))]


def render(label, target=None, broken=False):
    global worker
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    assert not status["error"], status
    if worker is None:
        worker = status["renderer_pid"]
    assert status["renderer_pid"] == worker
    plane = frame["planes"]["combined"]
    actual = (
        np.frombuffer(plane["pixels"], np.float32).reshape(32, 32, plane["channels"])[14:18, 14:18, :3].mean((0, 1))
    )
    if broken:
        assert any(d["code"] == "world.unavailable" for d in status["source_diagnostics"]), status
    else:
        assert not status["source_diagnostics"], status
        target = expected() if target is None else np.asarray(target)
        np.testing.assert_allclose(actual, target, atol=0.002, rtol=0, err_msg=label)
    assert status["geometry_conversions"] == 0, status
    report["checks"].append(
        dict(
            label=label,
            actual=actual.tolist(),
            expected=None if broken else target.tolist(),
            generation=status["generation"],
        )
    )


try:
    # These four expected colors were independently captured from Houdini's
    # native viewport flipbook: green, red, yellow, blue at these yaw angles.
    for yaw, band in ((45, 1), (135, 0), (225, 3), (315, 2)):
        cam.parmTuple("r").set((0, yaw, 0))
        render("Viewport cardinal " + str(yaw), labels[band])
    for rotation in ((0, 90, 0), (0, -90, 0), (0, 180, 0), (25, 35, -15), (-30, 70, 20)):
        env.parmTuple("r").set(rotation)
        cam.parmTuple("r").set((15, 45, 0))
        render("Live environment rotation " + str(rotation))
    rig.parmTuple("r").set((15, -25, 10))
    env.parmTuple("r").set((-30, 70, 20))
    render("Parent and local rotations")
    rig.parmTuple("t").set((7, -5, 13))
    rig.parm("scale").set(2)
    render("Translation and uniform scale retain orientation")
    rig.parmTuple("r").set((0, 0, 0))
    rig.parmTuple("t").set((0, 0, 0))
    rig.parm("scale").set(1)
    env.parmTuple("r").set((0, 0, 0))
    for pitch, color in ((80, (0.7, 0.1, 0.7)), (-80, (0.1, 0.7, 0.7))):
        cam.parmTuple("r").set((pitch, 25, 0))
        render("Native pole " + str(pitch), color)
    cam.parmTuple("r").set((0, 45, 0))
    env.parm("env_map").set(str(dest / "missing-orientation.exr"))
    render("Missing image is recoverable", broken=True)
    env.parm("env_map").set(str(path))
    render("Image repaired during IPR", labels[1])
    env.parm("ry").set(90)
    render("Rotation after repair")
    env.parm("ry").set(0)
    render("Rotation restored after repair", labels[1])
    entry.session.cancel()
    render("Stop and restart retains corrected orientation", labels[1])
    builder = create_world_material(name="unconverted_world_material")
    source = builder.createNode("cycles::geometry::1", "ray_direction")
    texture = builder.createNode("cycles::environment_texture::1", "unconverted_image")
    texture.setParms(dict(filename=str(path), colorspace="data"))
    texture.setNamedInput("vector", source, "position")
    builder.node("background_shader").setNamedInput("color", texture, "color")
    rop.setParms(dict(world_source="material", world_material=builder.path()))
    cam.parmTuple("r").set((20, 45, 0))
    render("Authored World Material retains Cycles coordinates", labels[0])
    env.parm("ry").set(90)
    render("Native light rotation does not affect authored World Material", labels[0])
    report["result"] = "passed"
finally:
    registry.close_all()
    (args.report or dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]), device=args.device)))
