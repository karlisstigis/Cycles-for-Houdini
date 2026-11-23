# SPDX-License-Identifier: Apache-2.0
"""Owned COP environment images, nominal shutter policy and live source recovery."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, json, os, time, traceback
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, menu_actions, light_properties, _native
from cycles_houdini.materials import create_world_material

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/world/cop"
dest.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
legacy = hou.node("/img").createNode("img", "world_cops")
source = legacy.createNode("color", "radiance")
source.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
source.parmTuple("color").set((0.25, 0.5, 0.75, 1))
output = legacy.createNode("null", "OUT")
output.setInput(0, source)
modern = hou.node("/img").createNode("copnet", "world_copernicus")
modern.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
constant = modern.createNode("constant", "radiance")
constant.parm("signature").set("f3")
constant.parmTuple("f3").set((0.5, 0.25, 0.125))
scene = hou.node("/obj").createNode("subnet", "cop_world")
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=2, shutter=0.5))
environment = scene.createNode("envlight", "environment")
light_properties.install(environment)
environment.setParms(
    dict(
        env_map="op:" + output.path(),
        light_contribprimary=1,
        light_intensity=1,
        light_exposure=0,
        cycles_environment_colorspace="data",
    )
)
environment.parmTuple("light_color").set((1, 1, 1))
world = create_world_material(name="cop_world_material", texture="op:" + output.path())
image = world.node("environment_texture")
image.parm("colorspace").set("data")
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
        ipr_samples=128,
        world_source="material",
        world_material=world.path(),
        use_denoise=0,
        ipr_use_denoise=0,
        film_exposure=1,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
        motion_samples=3,
        render_threads=2,
        use_texture_cache=0,
    )
)
fallback = (0.03125, 0.0625, 0.125)
rop.parmTuple("background").set(fallback)
config = native_scene.configuration(rop)
entry = registry.for_rop(rop, config)
entry.session.close()
entry.session = registry.session_from_configuration(config, collect_diagnostics=True)
report = dict(result="running", device=device, checks=[])
notified = []
observer = native_scene.SceneObserver(rop, lambda: notified.append(True))


def center(frame):
    plane = frame["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(64, 96, 4)
    assert np.isfinite(pixels).all()
    return pixels[31:33, 47:49, :3].mean((0, 1))


def render(name, expected, code=None, at=None):
    at = hou.time() if at is None else at
    before = (source.cookCount(), constant.cookCount())
    scene_update = native_scene.capture(rop, at)
    assert (source.cookCount(), constant.cookCount()) == before, "World dependency inspection cooked COP pixels"
    frame = entry.session.wait(entry.apply(scene_update, at))
    status = entry.session.status()
    assert not status["error"] and status["render_device_ids"] == [device], status
    measured = center(frame)
    assert np.allclose(measured, expected, atol=0.0002, rtol=0), (
        name,
        measured,
        expected,
        status["source_diagnostics"],
    )
    codes = {d["code"] for d in status["source_diagnostics"]}
    assert codes == ({code} if code else set()), (name, status["source_diagnostics"])
    report["checks"].append(
        dict(
            name=name,
            measured=measured.tolist(),
            diagnostics=status["source_diagnostics"],
            cop=_native.cop_capture_stats(entry.session._handle),
        )
    )
    return status


try:
    render("World material consumes a legacy COP environment", (0.25, 0.5, 0.75))
    copies = entry.session.diagnostics()["buffer_copy_requests"]
    assert copies["light_background_conditional_cdf"]["requested_bytes"] == (32 + 1) * 16 * 8, copies
    rop.parm("allowmotionblur").set(1)
    render("Static COP world survives geometry motion-blur enable", (0.25, 0.5, 0.75))
    rop.parm("world_source").set("scene")
    render("Native environment light consumes a COP image with blur enabled", (0.25, 0.5, 0.75))
    captures = _native.cop_capture_stats(entry.session._handle)["captures"]
    camera.parm("tx").set(0.125)
    render("Camera-only edits retain environment pixels", (0.25, 0.5, 0.75))
    assert _native.cop_capture_stats(entry.session._handle)["captures"] == captures
    observer.refresh()
    notified.clear()
    before = source.cookCount()
    source.parm("colorr").set(0.625)
    assert notified and source.cookCount() == before
    render("Uncooked upstream environment edits notify and update lighting", (0.625, 0.5, 0.75))
    environment.parm("ry").setExpression("$T*360", hou.exprLanguage.Hscript)
    render("Animated native environment transforms retain nominal lighting", (0.625, 0.5, 0.75), "world.motion_ignored")
    environment.parm("ry").deleteAllKeyframes()
    environment.parm("ry").set(0)
    render("Repairing native environment motion clears its warning", (0.625, 0.5, 0.75))
    source.parm("colorr").setExpression(".25+$T*12", hou.exprLanguage.Hscript)
    render(
        "Animated native COP world uses requested-frame radiance and reports omitted shutter sampling",
        (0.25, 0.5, 0.75),
        "world.motion_ignored",
    )
    rop.parm("world_source").set("material")
    render("Animated World Material COP keeps nominal lighting", (0.25, 0.5, 0.75), "world.motion_ignored")
    before = hou.time()
    render(
        "Explicit frame capture evaluates COP world without changing the timeline",
        (0.75, 0.5, 0.75),
        "world.motion_ignored",
        hou.frameToTime(2),
    )
    assert hou.time() == before
    image.parm("filename").set("op:" + output.path() + "[2]")
    render("Pinned-frame COP world is static across the shutter", (0.75, 0.5, 0.75))
    image.parm("filename").set("op:" + constant.path())
    render("COPernicus environment radiance reaches the world", (0.5, 0.25, 0.125))
    world.node("background_shader").parm("strength").setExpression("2+$T*12", hou.exprLanguage.Hscript)
    render(
        "Animated world strength retains nominal shading instead of discarding the world",
        (1, 0.5, 0.25),
        "world.motion_ignored",
    )
    world.node("background_shader").parm("strength").deleteAllKeyframes()
    world.node("background_shader").parm("strength").set(1)
    constant.parm("f3r").setExpression(".5+$T*6", hou.exprLanguage.Hscript)
    render("Animated Copernicus world uses the same nominal policy", (0.5, 0.25, 0.125), "world.motion_ignored")
    constant.parm("f3r").deleteAllKeyframes()
    constant.parm("f3r").set(0.5)
    render("Removing COP animation clears its warning on the first accepted update", (0.5, 0.25, 0.125))
    constant.parm("f3r").setExpression(".5+$T*6", hou.exprLanguage.Hscript)
    rop.parm("allowmotionblur").set(0)
    render("Disabling shutter sampling clears motion warnings", (0.5, 0.25, 0.125))
    # Missing world textures never become a magenta scene light.
    image.parm("filename").set("op:/img/absent_world/source")
    render("Missing world COP falls back to configured radiance", fallback, "world.unavailable")
    observer.refresh()
    notified.clear()
    repaired = hou.node("/img").createNode("copnet", "absent_world")
    repaired.setParms(dict(setres=1, res1=32, res2=16))
    replacement = repaired.createNode("constant", "source")
    replacement.parm("signature").set("f3")
    replacement.parmTuple("f3").set((0.125, 0.25, 0.5))
    assert notified
    render("Creating the missing source restores its world and clears the warning", (0.125, 0.25, 0.5))
    # Check actual illumination, rather than just a visible background.
    geo = scene.createNode("geo", "receiver", run_init_scripts=False)
    grid = geo.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((8, 8))
    grid.setDisplayFlag(True)
    grid.setRenderFlag(True)
    rop.setParms(dict(vobject="receiver", material_kind="diffuse", use_material_assignments=0))
    rop.parmTuple("basecolor").set((1, 1, 1))
    rop.parm("roughness").set(0)
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    assert np.allclose(center(frame), (0.125, 0.25, 0.5), atol=0.025, rtol=0), (center(frame), status)
    conversions = status["geometry_conversions"]
    replacement.parmTuple("f3").set((0.5, 0.25, 0.125))
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    assert np.allclose(center(frame), (0.5, 0.25, 0.125), atol=0.025, rtol=0)
    assert entry.session.status()["geometry_conversions"] == conversions
    report["checks"].append(
        dict(name="COP world illuminates Lambertian geometry and uses actual image dimensions for MIS")
    )
    rop.setParms(dict(picture=str(dest / "world.$F4.exr"), vobject=""))
    replacement.parm("f3r").setExpression(".125+$F*.125", hou.exprLanguage.Hscript)
    rop.render(frame_range=(1, 5, 1))
    import OpenImageIO as oiio

    for frame in range(1, 6):
        reader = oiio.ImageInput.open(str(dest / ("world.%04d.exr" % frame)))
        assert reader
        spec = reader.spec()
        pixels = np.asarray(reader.read_image(), np.float32)
        reader.close()
        indices = [list(spec.channelnames).index(c) for c in ("R", "G", "B")]
        assert np.allclose(pixels[32, 48, indices], (0.125 + frame * 0.125, 0.25, 0.125), atol=0.0002, rtol=0)
        assert not json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
    hou.setFrame(1)
    report.update(result="passed", native_batch_frames=5)
    hou.hipFile.save(str(dest / "cop-world.hiplc"))
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    observer.close()
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]), native_batch_frames=5)))
