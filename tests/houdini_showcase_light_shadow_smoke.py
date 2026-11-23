# SPDX-License-Identifier: Apache-2.0
"""Separate sky-sun shadows from the movable showcase area light."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
from PySide6 import QtGui
from cycles_houdini import native_scene, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/light-properties/showcase-shadows"
destination.mkdir(parents=True, exist_ok=True)
hou.hipFile.load(
    str(artifacts_root() / "examples/sop-fx-courtyard/sop-fx-courtyard-adaptive.hiplc"), suppress_save_prompt=True
)
hou.putenv("OCIO", str(artifacts_root() / "examples/sop-fx-courtyard/showcase.ocio"))
hou.Color.reloadOCIO()
rop = hou.node("/out/cycles_sop_courtyard")
light = hou.node("/obj/light1")
set_rop_devices(rop, device)
rop.setParms(dict(resx=480, resy=270, samples=32, ipr_samples=32, use_denoise=0, ipr_use_denoise=0))
entry = registry.for_rop(rop, native_scene.configuration(rop))
original = light.parmTuple("t").eval()
images = {}
captures = {}
sky_graph = native_scene.world.configuration(rop, hou.time())


def render(name):
    update = native_scene.capture(rop)
    captures[name] = update["lights"]
    frame = entry.session.wait(entry.apply(update, hou.time()))
    p = frame["planes"]["combined"]
    pixels = np.frombuffer(p["pixels"], np.float32).reshape(270, 480, 4).copy()
    images[name] = pixels
    # Use the same active OCIO display/view as the viewer, with fixed exposure.
    display = entry.session.display_image()
    image = QtGui.QImage(display["rgba"], 480, 270, 480 * 4, QtGui.QImage.Format_RGBA8888).copy()
    assert image.save(str(destination / (name + ".png")))


try:
    render("original")
    light.parm("light_enable").set(0)
    render("sky_only")
    light.parm("light_enable").set(1)
    light.parm("tx").set(original[0] + 8)
    render("moved_with_sky")
    rop.parm("world_source").set("color")
    rop.parmTuple("background").set((0, 0, 0))
    render("moved_area_only")
    light.parmTuple("t").set(original)
    render("original_area_only")
    delta = float(np.mean(np.abs(images["original_area_only"] - images["moved_area_only"])))
    assert delta > 0.001, ("Showcase area lighting did not move", delta)
    report = dict(
        result="passed",
        device_ids=entry.session.status()["render_device_ids"],
        area_movement_delta=delta,
        sky_only_difference=float(np.mean(np.abs(images["original"] - images["sky_only"]))),
        captures=captures,
        sky_graph=sky_graph,
    )
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
finally:
    registry.close_all()
