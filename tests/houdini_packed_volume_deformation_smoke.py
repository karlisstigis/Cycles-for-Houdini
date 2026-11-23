# SPDX-License-Identifier: Apache-2.0
"""Deforming mesh and static sparse volume in each shared packed prototype."""

from native_test_runtime import runtime_executable
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from packed_deformation_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
f = Fixture(args.device)
f.width, f.height = 320, 180
f.dest = f.dest / "mixed-volume"
f.dest.mkdir(parents=True, exist_ok=True)
path = f.dest / "report.json"
report = dict(result="running", device=args.device, checks=[])
f.grid.parmTuple("size").set((0.4, 0.6))
mesh = f.obj.createNode("xform", "mesh_left")
mesh.setInput(0, f.motion)
mesh.parm("tx").set(-1)
fixture = f.dest / "density.vdb"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-volume-fixture", str(fixture)], check=True)
file = f.obj.createNode("file", "density_file")
file.parm("file").set(str(fixture))
volume = f.obj.createNode("stash", "owned_density")
volume.parm("stash").set(file.geometry())
file.destroy()
fixture.unlink()
shift = f.obj.createNode("xform", "density_right")
shift.setInput(0, volume)
shift.parm("tx").set(1)
medium = ch.create_material(name="packed_absorption", shader="absorption_volume")
shader = medium.node("volume_shader")
shader.parmTuple("color").set((0, 0, 0))
density_control = medium.node("density_scale").parm("value2")
assigned = f.obj.createNode("material", "density_material")
assigned.setInput(0, shift)
assigned.parm("shop_materialpath1").set(medium.path())
merged = f.obj.createNode("merge", "mesh_and_volume")
merged.setInput(0, mesh)
merged.setInput(1, assigned)
packed = f.obj.createNode("pack", "packed_mesh_and_volume")
packed.setInput(0, merged)
packed.parm("pivot").set("origin")
targets = f.obj.createNode("line", "two_placements")
targets.parm("points").set(2)
targets.parmTuple("origin").set((0, -0.8, 0))
targets.parm("dist").set(1.6)
oriented = f.obj.createNode("attribwrangle", "placement_orientation")
oriented.setInput(0, targets)
oriented.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
copies = f.obj.createNode("copytopoints", "mixed_instances")
copies.setInput(0, packed)
copies.setInput(1, oriented)
clean = f.obj.createNode("attribdelete", "implicit_placement_identities")
clean.setInput(0, copies)
clean.parm("primdel").set("id name")
rop = f.rop(clean)
rop.parmTuple("background").set((1, 1, 1))


def pixel(frame, x, y):
    pixels = f.pixels(frame)
    column = round((x / 4 + 0.5) * frame["width"])
    row = round((y / 2.25 + 0.5) * frame["height"])
    return pixels[row - 1 : row + 2, column - 1 : column + 2, :3].mean(axis=(0, 1))


try:
    with ch.Session(
        device=args.device,
        width=f.width,
        height=f.height,
        samples=1024,
        threads=2,
        interactive=False,
        collect_diagnostics=True,
    ) as session:
        baseline = None
        for name, broken, density in [
            ("initial", 0, 1),
            ("warm", 0, 1),
            ("broken mesh shutter", 1, 1),
            ("repair mesh shutter", 0, 1),
            ("edit absorption", 0, 0.5),
            ("restore absorption", 0, 1),
        ]:
            f.motion.parm("test_broken").set(broken)
            density_control.set(density)
            frame = session.wait(session.apply(native_scene.capture(rop), at_time=0))
            status = session.status()
            assert status["state"] == "complete" and not status["error"], status
            assert status["render_device_ids"] == [args.device]
            assert status["geometry_resources"] == 2 and status["object_resources"] == 4, status
            codes = {value["code"] for value in status["source_diagnostics"]}
            if baseline is None:
                baseline = codes
            if not broken:
                assert codes == baseline, status["source_diagnostics"]
            else:
                assert status["source_diagnostics"], status
            coverage = 1 if broken else 0.4
            expected_surface = f.color * coverage + np.ones(3) * (1 - coverage)
            # Independent nine-voxel fixture: integrated centre density = 9/8.
            expected_volume = math.exp(-density * 9 / 8)
            values = []
            for y in (-0.8, 0.8):
                surface, attenuation = pixel(frame, -1, y), pixel(frame, 1, y)
                assert np.allclose(surface, expected_surface, atol=0.055), (name, y, surface, expected_surface)
                assert np.allclose(attenuation, expected_volume, atol=0.025), (name, y, attenuation, expected_volume)
                values.append(dict(y=y, surface=surface.tolist(), transmittance=attenuation.tolist()))
            report["checks"].append(dict(name=name, measured=values, status=status))
            path.write_text(json.dumps(report, indent=2))
        session.write_exr(str(f.dest / "packed-volume.exr"))
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
