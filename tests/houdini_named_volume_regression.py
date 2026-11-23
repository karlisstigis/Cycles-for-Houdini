# SPDX-License-Identifier: Apache-2.0
"""First managed volume must never inherit the world object's sampling policy."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import math
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/volumes"
destination.mkdir(parents=True, exist_ok=True)
path = destination / "named-volume-regression.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-pyro-fixture", str(path)], check=True)
obj = hou.node("/obj").createNode("geo", "first_volume", run_init_scripts=False)
file = obj.createNode("file")
file.parm("file").set(str(path))
stash = obj.createNode("stash")
stash.parm("stash").set(file.geometry())
file.destroy()
path.unlink()
camera = hou.node("/obj").createNode("cam", "named_volume_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
builder = ch.create_material(shader="principled_volume")
shader = builder.node("volume_shader")
shader.parmTuple("color").set((0, 0, 0))
shader.parm("density").set(1.0)
constant = builder.createNode("cycles::value::1", "constant_density")
constant.parm("value").set(1.0)
field = builder.createNode("cycles::volume_info::1", "explicit_density")
reports = []
for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=80, height=48, samples=512) as session:
        checks = []
        for moving in (False, True):
            settings = dict(enabled=moving, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3)
            for mode in ("named", "linked_constant", "explicit_field", "convenience"):
                shader.setInput(shader.inputIndex("density"), None)
                shader.parm("density_attribute").set("" if mode == "explicit_field" else "density")
                if mode == "linked_constant":
                    shader.setNamedInput("density", constant, "value")
                if mode == "explicit_field":
                    shader.setNamedInput("density", field, "density")
                material = (
                    dict(key="medium", kind="principled_volume", color=(0, 0, 0), density=1.0)
                    if mode == "convenience"
                    else dict(key="medium", kind="graph", graph=ch.material_graph(builder))
                )
                request = ch.geometry_request(
                    obj, stash, material="medium", motion="velocity" if moving else "transform"
                )
                frame = session.wait(
                    session.apply(
                        dict(
                            geometry=[request],
                            materials=[material],
                            camera_path=camera.path(),
                            motion=settings,
                            background=(1, 1, 1),
                        )
                    )
                )
                pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(48, 80, 4)
                measured = pixels[21:27, 37:43, :3].mean(axis=(0, 1)).tolist()
                # Nine voxel centres at spacing 1/8, integrated trilinearly.
                # This fixture's slow advection stays within its centre plateau.
                expected = math.exp(-9 / 8)
                assert max(abs(value - expected) for value in measured) < 0.025, (
                    device,
                    moving,
                    mode,
                    measured,
                    expected,
                )
                state = session.status()
                assert state["volume_ray_marching"] == moving
                assert state["geometry_resources"] == state["object_resources"] == 1
                checks.append(dict(mode=mode, motion=moving, measured=measured, expected=expected, status=state))
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    reports.append(dict(device=device, result="passed", checks=checks, close=closed))
report = dict(
    result="passed",
    devices=reports,
    color_policy=ch.color_policy(),
    scope="One first managed sparse volume, constant/linked named density, explicit Volume Info and convenience material, static octree and moving ray-march policies. This regresses the missing-background-object defect.",
)
(destination / "houdini-named-volume-regression.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks_per_device=[len(row["checks"]) for row in reports])))
