# SPDX-License-Identifier: Apache-2.0
"""SOHO frame evaluation and associated-alpha output on both native devices."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/transparency/batch"
destination.mkdir(parents=True, exist_ok=True)
original_env = hou.getenv("OCIO")
config = OCIO.Config.CreateFromStream(OCIO.GetCurrentConfig().serialize())
config.setWorkingDir(OCIO.GetCurrentConfig().getWorkingDir())
config.setRole(OCIO.ROLE_SCENE_LINEAR, "ACEScg")
profile = destination / "batch.ocio"
profile.write_text(config.serialize(), encoding="utf-8")
hou.putenv("OCIO", str(profile))
hou.Color.reloadOCIO()
reports = []
try:
    hou.hipFile.load(str(artifacts_root() / "stage3/color/transparency/native-alpha.hiplc"), suppress_save_prompt=True)
    rop = hou.node("/out/native_alpha_cycles")
    # Each render selects one SOP. The analytic surface image must exclude
    # the independent emissive volume in the same HIP.
    rop.parm("source_mode").set("sop")
    rop.parm("transparent_background").setExpression("$F % 2", hou.exprLanguage.Hscript)
    rop.parmTuple("background").set((0.05, 0.1, 0.15))
    # The sparse boundary's stochastic step offset needs the same 1024-sample
    # convergence budget as the independent analytic headless fixture.
    rop.parm("samples").set(1024)
    rop.parm("adaptive_sampling").set(0)
    # Compare the linear integrator output against analytic radiance and alpha.
    # Final-render denoising can filter RGB independently of the coverage plane.
    rop.parm("use_denoise").set(0)
    for device in ("CPU", ch.default_device()):
        set_rop_devices(rop, device)
        label = "cpu" if device == "CPU" else "optix"
        for kind, sop in (
            ("surface", "/obj/native_alpha_geometry/half_plane"),
            ("emission", "/obj/native_alpha_volume/stash1"),
        ):
            rop.parm("soppath").set(sop)
            rop.parm("picture").set(str(destination / f"{label}-{kind}.$F4.exr"))
            rop.render(frame_range=(1, 2, 1))
            frames = []
            for frame in (1, 2):
                path = destination / f"{label}-{kind}.{frame:04}.exr"
                reader = OIIO.ImageInput.open(str(path))
                assert reader, path
                spec = reader.spec()
                image = reader.read_image(format=OIIO.FLOAT)
                pixels = image[..., [spec.channelnames.index(c) for c in ("R", "G", "B", "A")]]
                assert np.isfinite(pixels).all()
                assert spec.get_string_attribute("colorInteropID") == "lin_ap1_scene"
                transparent = frame == 1
                np.testing.assert_allclose(
                    pixels[31, 84], np.zeros(4) if transparent else [0.05, 0.1, 0.15, 1], rtol=0, atol=2e-5
                )
                if kind == "surface":
                    np.testing.assert_allclose(pixels[31, 12], [0.4, 0.2, 0.1, 1], rtol=0, atol=2e-5)
                    if transparent:
                        np.testing.assert_allclose(
                            pixels[..., :3], pixels[..., 3, None] * [0.4, 0.2, 0.1], rtol=0, atol=2e-5
                        )
                else:
                    expected = np.asarray([0.5, 0.25, 0.125]) * 9 / 8
                    if not transparent:
                        expected += [0.05, 0.1, 0.15]
                    np.testing.assert_allclose(pixels[31, 48, :3], expected, rtol=0, atol=0.01)
                    assert pixels[31, 48, 3] == (0 if transparent else 1)
                reader.close()
                frames.append(
                    dict(
                        frame=frame,
                        transparent=transparent,
                        output=str(path),
                        background_rgba=pixels[31, 84].tolist(),
                        foreground_rgba=pixels[31, 12 if kind == "surface" else 48].tolist(),
                    )
                )
            reports.append(dict(device=device, geometry=kind, frames=frames))
    report = dict(
        result="passed",
        cases=reports,
        scope="SOHO re-evaluates animated film transparency and preserves float32 surface/volume RGBA",
    )
    (destination / "houdini-transparency-batch-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
finally:
    if original_env is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_env)
    hou.Color.reloadOCIO()
