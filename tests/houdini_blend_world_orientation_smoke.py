# SPDX-License-Identifier: Apache-2.0
"""Blender world orientation gate: an imported simple world shows Blender's marks in the
converted directions at Mapping Z rotation 0, 90 and 215 degrees (blend-import.md: Worlds).

Runs Blender (tests/fixtures/blend_worlds/orientation_reference.py) to write the marked
HDRI, the library .blend and Blender's own probe renders, imports each world through the
asset path (blend_worlds.prepare_world / build_world, Convert & Append, Replace), then
renders probe cameras in Houdini looking along C d (d = where Blender shows the mark,
C = the converter's Blender Z-up -> Houdini Y-up axes) and a mirror sphere for evidence.
"""

from support.paths import artifacts_root

import argparse
import importlib.util
import json
import math
import os
import subprocess
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from cycles_houdini.blender import blend_worker, blend_worlds
from cycles_houdini.core import blender_bridge
from cycles_houdini.device_selection import set_devices

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--report", type=Path)
p.add_argument("--blender", default=None, help="Blender executable (default: the bridge's)")
args = p.parse_args()
dest = Path(os.environ.get("CYCLES_HOUDINI_TEST_OUTPUT_ROOT", artifacts_root() / "stage4/world/blender_orientation"))
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
fixture = Path(__file__).resolve().parent / "fixtures/blend_worlds/orientation_reference.py"
spec = importlib.util.spec_from_file_location("orientation_reference", fixture)
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def convert(d):
    """blend_convert._CONVERT: Blender (x, y, z) -> Houdini (x, z, -y)."""
    return (d[0], d[2], -d[1])


def aim(direction):
    """Houdini camera rotation (rotate order xyz) looking along `direction`."""
    x, y, z = direction
    return (math.degrees(math.atan2(y, math.hypot(x, z))), math.degrees(math.atan2(-x, -z)), 0.0)


blender = args.blender or str(blender_bridge.require_executable())
# The library is authored the way an artist's Blender authors it: with Blender's own OCIO
# configuration, as the bridge starts Blender (not Houdini's, which hython exports as OCIO).
completed = subprocess.run(
    [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(fixture), "--", str(dest)],
    capture_output=True,
    text=True,
    timeout=900,
    env=blend_worker.blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
)
assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-4000:]
blender_report = json.loads((dest / "blender_report.json").read_text(encoding="utf-8"))
assert blender_report["ok"], blender_report
report["blender"] = blender_report
library = dest / "orientation_worlds.blend"

cam = hou.node("/obj").createNode("cam", "world_orientation_probe")
cam.parm("focal").set(1000)
ball = hou.node("/obj").createNode("geo", "world_orientation_mirror_ball")
sphere = ball.createNode("sphere")
sphere.parm("type").set("polymesh")
if sphere.parm("freq") is not None:
    sphere.parm("freq").set(32)
mirror = ch.create_material(name="world_orientation_mirror")
shader = mirror.node("surface_shader")
for name, value in (("metallic", 1.0), ("roughness", 0.0)):
    if shader.parm(name) is not None:
        shader.parm(name).set(value)
ball.parm("shop_materialpath").set(mirror.path())
top = hou.node("/obj").createNode("cam", "world_orientation_top")
# Blender's camera at +Z looking down (image up = Blender +Y), converted: C = R_x(-90).
top.parmTuple("t").set((0, 5, 0))
top.parmTuple("r").set((-90, 0, 0))
top.parm("projection").set("ortho")
top.parm("orthowidth").set(2.2)
rop = hou.node("/out").createNode("cycles_ipr", "world_orientation")
set_devices(rop, args.device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject="",
        camera=cam.path(),
        world_source="scene",
        resx=8,
        resy=8,
        samples=4,
        ipr_samples=4,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
    )
)


def render(size):
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    assert not status["error"], status
    assert not status["source_diagnostics"], status
    plane = frame["planes"]["combined"]
    return np.frombuffer(plane["pixels"], np.float32).reshape(size, size, plane["channels"])[..., :3]


try:
    asset_root = dest / "assets_blender"
    for angle in reference.ANGLES:
        name = reference.world_name(angle)
        prepared = blend_worlds.prepare_world(library, name, root=asset_root, executable=blender)
        assert prepared["route"] == "light", prepared["record"].get("reasons")
        result = blend_worlds.build_world(prepared, mode="append", existing_environment="replace")
        light = result["node"]
        expected = blend_worlds.light_rotation_for_blender_mapping(math.radians(angle))
        # Equal as angles: Houdini may hold -180 as +179.999997 (float parameters).
        turn = (np.asarray(light.parmTuple("r").eval()) - np.asarray(expected) + 180.0) % 360.0 - 180.0
        np.testing.assert_allclose(turn, 0.0, atol=1e-4)
        assert light.parm("cycles_environment_colorspace").evalAsString() == "data"
        rop.setParms(dict(lights=light.path(), vobject="", camera=cam.path(), resx=8, resy=8))
        for mark in reference.MARKS:
            direction = convert(reference.mark_direction(mark, angle))
            cam.parmTuple("r").set(aim(direction))
            rgb = render(8)[3:5, 3:5].mean((0, 1)).tolist()
            seen = reference.classify(rgb)
            check = dict(angle=angle, mark=mark, direction=direction, rgb=rgb, seen=seen)
            report["checks"].append(check)
            assert seen == mark, check
            assert blender_report["angles"][str(angle)][mark]["seen"] == mark
        rop.setParms(dict(vobject=ball.path(), camera=top.path(), resx=96, resy=96, use_material_assignments=1))
        image = render(96)
        out = oiio.ImageOutput.create(str(dest / f"houdini_{angle:03d}_mirror_ball.exr"))
        assert out.open(str(dest / f"houdini_{angle:03d}_mirror_ball.exr"), oiio.ImageSpec(96, 96, 3, oiio.FLOAT))
        assert out.write_image(np.ascontiguousarray(image)) and out.close()
    report["result"] = "passed"
finally:
    registry.close_all()
    (args.report or dest / "report.json").write_text(json.dumps(report, indent=2))
