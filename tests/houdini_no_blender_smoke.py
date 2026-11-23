# SPDX-License-Identifier: Apache-2.0
"""CPU first image without Blender; diagnose only an explicitly requested .blend asset."""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene
from cycles_houdini.blender import blend_worker
from support.paths import artifacts_root

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path, default=artifacts_root() / "optional-blender/report.json")
args = parser.parse_args()
assert args.device == "CPU", "This clean-machine first-image contract uses CPU"
args.report.parent.mkdir(parents=True, exist_ok=True)
missing = args.report.parent / "not-installed/blender.exe"
assert not missing.exists()
os.environ["CYCLES_HOUDINI_BLENDER"] = str(missing)
os.environ["CYCLES_HOUDINI_BLENDER_BRIDGE"] = "1"
hou.putenv("CYCLES_HOUDINI_BLENDER", str(missing))
hou.putenv("CYCLES_HOUDINI_BLENDER_BRIDGE", "1")
caps = ch.capabilities()
assert caps["blender_bridge"] is False and caps["blender_bridge_enabled"] is True, caps
assert caps["image_iris"] and caps["gaussian_splats"], caps

root = hou.node("/obj").createNode("subnet", "no_blender_first_image")
geo = root.createNode("geo", "native_geometry", run_init_scripts=False)
box = geo.createNode("box")
box.setDisplayFlag(True)
box.setRenderFlag(True)
camera = root.createNode("cam", "camera")
camera.parmTuple("t").set((0, 0, 5))
rop = hou.node("/out").createNode("cycles_ipr", "no_blender")
device_selection.set_devices(rop, "CPU")
image = args.report.parent / "first-image.exr"
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=root.path(),
        vobject="*",
        lights="",
        camera=camera.path(),
        resx=32,
        resy=24,
        samples=2,
        ipr_samples=2,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
        render_threads=2,
        material_kind="emission",
        picture=str(image),
    )
)
rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))

with patch.object(blend_worker._Worker, "__init__", side_effect=AssertionError("Blender worker started")):
    first = native_scene.capture(rop)
    assert not any("blender" in item["code"] for item in first.get("source_diagnostics", ())), first
    rop.render(frame_range=(hou.frame(), hou.frame()))
    source = oiio.ImageInput.open(str(image))
    assert source is not None, oiio.geterror()
    try:
        pixels = np.asarray(source.read_image(format=oiio.FLOAT))
    finally:
        source.close()
    assert np.isfinite(pixels).all() and pixels.shape[:2] == (24, 32), pixels.shape
    assert pixels[12, 16, :3].max() > 0.1, pixels[12, 16]

    # Only this requested asset should cause the actionable missing-Blender message.
    blend = args.report.parent / "requested.blend"
    blend.write_bytes(b"placeholder: discovery must fail before a worker starts")
    instance = root.createNode("geo", "requested_blend", run_init_scripts=False)
    point = instance.createNode("python")
    point.parm("python").set(
        "import hou\ng=hou.pwd().geometry()\n"
        "g.addAttrib(hou.attribType.Point, 'instance', '')\n"
        "p=g.createPoint()\np.setAttribValue('instance', " + repr(blend.as_posix() + "#object=Cube") + ")"
    )
    point.setDisplayFlag(True)
    point.setRenderFlag(True)
    diagnostics = {}
    for selected in ("1", "0"):
        os.environ["CYCLES_HOUDINI_BLENDER_BRIDGE"] = selected
        hou.putenv("CYCLES_HOUDINI_BLENDER_BRIDGE", selected)
        capture = native_scene.capture(rop)
        issues = [item for item in capture["source_diagnostics"] if "blender" in item["code"]]
        assert issues and all(item["source"] == instance.path() for item in issues), issues
        message = "\n".join(item["message"] for item in issues)
        if selected == "1":
            assert "Blender not found at " + str(missing) in message and "CYCLES_HOUDINI_BLENDER" in message, message
        else:
            assert "bridge is disabled" in message and "Setup again and select" in message, message
        diagnostics[selected] = issues

args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            device="CPU",
            capabilities=caps,
            first_image=str(image),
            first_image_without_blender=True,
            no_worker_started=True,
            requested_asset_diagnostics=diagnostics,
        ),
        indent=2,
    ),
    encoding="utf-8",
)
print(json.dumps(dict(result="passed", report=str(args.report))))
