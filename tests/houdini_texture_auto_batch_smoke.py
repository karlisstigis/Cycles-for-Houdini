# SPDX-License-Identifier: Apache-2.0
"""SOHO sequences re-evaluate Auto inputs and write diagnostic frames for broken sources."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import argparse
import os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/auto/batch"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_env = hou.getenv("OCIO")
original_mode = hou.getenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
rgb = [0.75, 0.375, 0.09375]
rec709 = "Linear Rec.709 (sRGB)"
srgb = "sRGB Encoded Rec.709 (sRGB)"
config = OCIO.Config.CreateFromStream(original.serialize())
config.setWorkingDir(original.getWorkingDir())
config.setRole(OCIO.ROLE_SCENE_LINEAR, "ACEScg")
rules = config.getFileRules()
rules.insertRule(0, "Native Auto Frame Two", srgb, "*frame.2*", "exr")
config.setFileRules(rules)
config.validate()
profile = destination / "native-auto-batch.ocio"
profile.write_text(config.serialize(), encoding="utf-8")
hou.putenv("OCIO", str(profile))
hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "4")
hou.Color.reloadOCIO()
config = OCIO.GetCurrentConfig()
for frame in (1, 2):
    path = destination / f"frame.{frame}.exr"
    output = OIIO.ImageOutput.create(str(path))
    spec = OIIO.ImageSpec(256, 256, 3, OIIO.FLOAT)
    spec.attribute("colorInteropID", "lin_rec709_scene" if frame == 1 else "lin_ap0_scene")
    assert output.open(str(path), spec)
    assert output.write_image(np.broadcast_to(np.asarray(rgb, np.float32), (256, 256, 3)).copy()) and output.close()

reports = []
parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
try:
    hou.hipFile.load(str(artifacts_root() / "stage3/textures/native-textures.hiplc"), suppress_save_prompt=True)
    rop = hou.node("/out/native_texture_cycles")
    texture = hou.node("/mat/native_texture_surface/file_image")
    texture.parm("colorspace").set("auto")
    texture.parm("filename").set(str(destination / "frame.$F.exr"))
    rop.parm("samples").set(8)
    for device in (args.device,):
        for cache in (False, True):
            label = ("cpu" if device == "CPU" else "optix") + ("-cache" if cache else "-uncached")
            output_dir = destination / label
            output_dir.mkdir(exist_ok=True)
            set_rop_devices(rop, device)
            rop.parm("use_texture_cache").set(int(cache))
            rop.parm("auto_texture_cache").set(int(cache))
            rop.parm("picture").set(str(output_dir / "sequence.$F4.exr"))
            rop.render(frame_range=(1, 2, 1))
            frames = []
            for frame, space in ((1, rec709), (2, srgb)):
                path = output_dir / f"sequence.{frame:04}.exr"
                reader = OIIO.ImageInput.open(str(path))
                assert reader, path
                spec = reader.spec()
                image = reader.read_image(format=OIIO.FLOAT)
                measured = [float(image[32, 32, spec.channelnames.index(component)]) for component in ("R", "G", "B")]
                expected = config.getProcessor(space, OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB(rgb)
                assert np.allclose(measured, expected, rtol=0, atol=0.001), (measured, expected)
                assert spec.get_string_attribute("oiio:ColorSpace") == "ACEScg"
                assert spec.get_string_attribute("colorInteropID") == "lin_ap1_scene"
                attributes = {value.name: value.value for value in spec.extra_attribs}
                assert "chromaticities" not in attributes and "acesImageContainer" not in attributes
                reader.close()
                frames.append(
                    dict(frame=frame, input_space=space, measured=measured, expected=expected, output=str(path))
                )
            reports.append(dict(device=device, cache=cache, result="passed", frames=frames))
    missing = destination / "missing.exr"
    missing.unlink(missing_ok=True)
    texture.parm("filename").set(str(missing))
    sentinel = destination / "diagnostic.exr"
    previous = b"previous complete frame"
    sentinel.write_bytes(previous)
    rop.parm("picture").set(str(sentinel))
    rop.render(frame_range=(1, 1, 1))
    reader = OIIO.ImageInput.open(str(sentinel))
    assert reader, sentinel
    spec = reader.spec()
    pixels = reader.read_image(format=OIIO.FLOAT)
    measured = [float(pixels[32, 32, spec.channelnames.index(channel)]) for channel in ("R", "G", "B")]
    assert np.allclose(measured, (1, 0, 1), atol=0.001, rtol=0), measured
    diagnostics = spec.get_string_attribute("cycles_houdini:source_diagnostics")
    assert any(
        missing.name in issue["message"] or missing.name in issue["source"] for issue in json.loads(diagnostics)
    ), diagnostics
    reader.close()
    assert sentinel.read_bytes() != previous
    output = OIIO.ImageOutput.create(str(missing))
    spec = OIIO.ImageSpec(256, 256, 3, OIIO.FLOAT)
    spec.attribute("colorInteropID", "lin_rec709_scene")
    assert output.open(str(missing), spec)
    assert output.write_image(np.broadcast_to(np.asarray(rgb, np.float32), (256, 256, 3)).copy()) and output.close()
    repaired = destination / "repaired.exr"
    rop.parm("picture").set(str(repaired))
    rop.render(frame_range=(1, 1, 1))
    reader = OIIO.ImageInput.open(str(repaired))
    assert reader, repaired
    spec = reader.spec()
    pixels = reader.read_image(format=OIIO.FLOAT)
    measured = [float(pixels[32, 32, spec.channelnames.index(channel)]) for channel in ("R", "G", "B")]
    expected = config.getProcessor(rec709, OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB(rgb)
    assert np.allclose(measured, expected, atol=0.001, rtol=0), (measured, expected)
    assert json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]") == []
    reader.close()
    texture.parm("filename").set(str(destination / "frame.$F.exr"))
    rop.parm("picture").set(str(destination / "example.$F4.exr"))
    set_rop_devices(rop, "CPU")
    hou.hipFile.save(str(destination / "native-auto-batch.hiplc"))
    report = dict(
        result="passed",
        cases=reports,
        missing_asset="Diagnostic frame written; repair clears the warning",
        diagnostic=str(sentinel),
        repaired=str(repaired),
        example=str(destination / "native-auto-batch.hiplc"),
        example_ocio=str(profile),
    )
    (destination / "houdini-texture-auto-batch-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
finally:
    if original_env is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_env)
    if original_mode is None:
        hou.unsetenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
    else:
        hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", original_mode)
    hou.Color.reloadOCIO()
