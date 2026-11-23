# SPDX-License-Identifier: Apache-2.0
"""SOHO native OBJ selection and explicit-time sequence output without Solaris."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from native_object_fixture import fixture

destination = artifacts_root() / "stage3/native-scene/batch"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
f = fixture()
# Verify scene translation in raw linear pixels, independently of denoising.
f.rop.setParms(dict(use_denoise=0, ipr_use_denoise=0, adaptive_sampling=0, ipr_adaptive_sampling=0))
f.red.parm("tdisplay").set(1)
f.red.parm("display").setExpression("$F < 2", hou.exprLanguage.Hscript)
f.blue.parm("tx").setExpression("1 + ($F-1)*.2", hou.exprLanguage.Hscript)
f.light.parm("light_exposure").setExpression("$F-1", hou.exprLanguage.Hscript)
reports = []
for device in ("CPU", ch.default_device()):
    label = "cpu" if device == "CPU" else "optix"
    set_rop_devices(f.rop, device)
    f.rop.parm("picture").set(str(destination / f"{label}.$F4.exr"))
    f.rop.render(frame_range=(1, 3, 1))
    checks = []
    for frame in (1, 2, 3):
        path = destination / f"{label}.{frame:04}.exr"
        image = OIIO.ImageInput.open(str(path))
        assert image, path
        spec = image.spec()
        pixels = image.read_image(format=OIIO.FLOAT)
        image.close()
        assert np.isfinite(pixels).all()
        channels = [spec.channelnames.index(name) for name in ("R", "G", "B", "A")]
        # EXR rows run from top to bottom. Both sources have world Y=0.
        red = pixels[46:50, 30:34][:, :, channels].mean((0, 1))
        expected = (*f.colors["red"], 1) if frame == 1 else (0, 0, 0, 0)
        assert np.allclose(red, expected, atol=0.001, rtol=0), (device, frame, red, expected)
        x = round(96 + 6.4 * (frame - 1))
        blue = pixels[46:50, x - 1 : x + 2][:, :, channels].mean((0, 1))
        assert np.allclose(blue, (*f.colors["blue"], 1), atol=0.001, rtol=0), (device, frame, blue)
        assert spec.width == 128 and spec.height == 96
        checks.append(dict(frame=frame, path=str(path), red=red.tolist(), blue=blue.tolist()))
    assert hou.frame() == 1
    reports.append(dict(device=device, checks=checks))
report = dict(
    result="passed",
    devices=reports,
    scope="Native SOHO OBJ scene, animated selection/transforms, linear EXR sequence; current host frame stays 1",
)
(destination / "houdini-object-scene-batch-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", frames=6)))
