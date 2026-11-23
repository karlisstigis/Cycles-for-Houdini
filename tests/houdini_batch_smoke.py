# SPDX-License-Identifier: Apache-2.0
"""Render-time evaluation must work without moving Houdini's current frame."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from cycles_houdini import passes
import json
import os
from pathlib import Path
import numpy as np
import OpenImageIO as OIIO
import hou
from cycles_houdini import native_scene

root = artifacts_root() / "stage1/batch"
root.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
geometry = hou.node("/obj").createNode("geo", "batch_geometry", run_init_scripts=False)
geometry.parm("tx").setExpression("($F - 1) * 0.2", hou.exprLanguage.Hscript)
box = geometry.createNode("box")
box.parmTuple("size").set((0.5, 0.7, 0.4))
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "batch_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
light = hou.node("/obj").createNode("hlight", "batch_light")
light.parm("tx").setExpression("$F", hou.exprLanguage.Hscript)
rop = hou.node("/out").createNode("cycles_ipr", "batch_rop")
passes.add(rop, "depth")
set_rop_devices(rop, "CPU")
for name, value in dict(
    soppath=box.path(),
    camera=camera.path(),
    lightpath=light.path(),
    resx=160,
    resy=90,
    samples=16,
    picture=str(root / "frame.$F4.exr"),
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor")[0].setExpression("0.2 + ($F - 1) * 0.1", hou.exprLanguage.Hscript)
capture = native_scene.capture(rop, at_time=hou.frameToTime(3))
assert abs(capture["geometry"][0]["transform"][3] - 0.4) < 1e-5
assert abs(capture["materials"][0]["color"][0] - 0.4) < 1e-5
assert abs(capture["lights"][0]["transform"][3] - 3) < 1e-5
assert hou.frame() == 1
for frame in (1, 2, 3):
    target = root / f"frame.{frame:04}.exr"
    if target.exists():
        target.unlink()
rop.render(frame_range=(1, 3, 1))
centroids = []
for frame in (1, 2, 3):
    target = root / f"frame.{frame:04}.exr"
    image = OIIO.ImageInput.open(str(target))
    assert image is not None, target
    spec = image.spec()
    pixels = image.read_image(format=OIIO.FLOAT)
    depth = pixels[:, :, spec.channelnames.index("depth.Z")]
    locations = np.nonzero((depth > 0) & (depth < 6))
    assert len(locations[1]) > 0, target
    centroid = float(np.mean(locations[1]))
    centroids.append(centroid)
    assert abs(centroid - (79.5 + (frame - 1) * 8)) < 0.7, (frame, centroid)
    assert np.max(np.abs(depth[locations] - 4.8)) < 0.001
    image.close()
report = dict(
    result="passed",
    frames=[1, 2, 3],
    centroids=centroids,
    explicit_time="OBJ, material and light evaluated at frame 3 while current frame was 1",
    sequence="SOHO batch output names and geometry projection passed",
)
(root / "houdini-batch-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
