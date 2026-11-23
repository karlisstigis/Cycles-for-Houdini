# SPDX-License-Identifier: Apache-2.0
"""Coverage alpha is independent of arbitrary OCIO display transforms."""

from support.paths import artifacts_root
from array import array
import json
import math
import os
from pathlib import Path

import hou
import PyOpenColorIO as OCIO
import cycles_houdini as ch


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/display-alpha"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_env = hou.getenv("OCIO")
reports = []


def profile(working, name):
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
    space = OCIO.ColorSpace(name="Coverage Display Regression")
    space.setTransform(
        OCIO.MatrixTransform(matrix=[0.5, 0, 0, 0, 0, 0.75, 0, 0, 0, 0, 0.25, 0, 0, 0, 0, 0.125]),
        OCIO.COLORSPACE_DIR_FROM_REFERENCE,
    )
    config.addColorSpace(space)
    config.addDisplayView("Coverage Test", "Coverage", space.getName(), "")
    config.setActiveDisplays("Coverage Test")
    config.setActiveViews("Coverage")
    path = destination / (name + ".ocio")
    path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(path))
    hou.Color.reloadOCIO()
    return OCIO.GetCurrentConfig()


def byte(value):
    assert math.isfinite(value), value
    return round(max(0, min(1, value)) * 255)


def check(session, frame, processor, label, exposure=0):
    before = session.status()
    source = array("f")
    source.frombytes(frame["planes"]["combined"]["pixels"])
    image = session.display_image(display="Coverage Test", view="Coverage", exposure=exposure)
    alphas = set()
    for y in range(frame["height"]):
        for x in range(frame["width"]):
            at = (y * frame["width"] + x) * 4
            rgba = source[at : at + 4]
            alpha = rgba[3]
            rgb = [value / alpha * 2**exposure if alpha > 1e-8 else 0 for value in rgba[:3]]
            expected = list(processor.applyRGB(rgb)) + [alpha]
            target = ((frame["height"] - 1 - y) * frame["width"] + x) * 4
            actual = list(image["rgba"][target : target + 4])
            assert all(abs(actual[c] - byte(expected[c])) <= 1 for c in range(3)), (label, x, y, actual, expected)
            assert actual[3] == byte(alpha), (
                label,
                "Display transform changed coverage alpha",
                x,
                y,
                actual[3],
                byte(alpha),
            )
            alphas.add(alpha)
    after = session.status()
    assert after["generation"] == before["generation"] == frame["generation"]
    assert after["geometry_conversions"] == before["geometry_conversions"]
    return dict(name=label, pixels=frame["width"] * frame["height"], source_alpha=sorted(alphas), exposure=exposure)


try:
    for index, device in enumerate(("CPU", ch.default_device())):
        source = profile("ACEScg", f"source-{index}")
        ch.synchronize_color_configuration()
        with ch.Session(device=device, width=32, height=18, samples=8) as session:
            frame = session.wait(session.calibration())
            processor = source.getProcessor(
                OCIO.ROLE_SCENE_LINEAR, "Coverage Test", "Coverage", OCIO.TRANSFORM_DIR_FORWARD
            ).getDefaultCPUProcessor()
            # Establish that this valid processor really can change alpha.
            assert abs(processor.applyRGBA([0.25, 0.5, 0.75, 1])[3] - 1) > 0.1
            checks = [
                check(session, frame, processor, "Current source configuration"),
                check(session, frame, processor, "Exposure preserves alpha", exposure=1),
            ]
            target = profile("Linear Rec.709 (sRGB)", f"target-{index}")
            ch.synchronize_color_configuration()
            processor = OCIO.Config.GetProcessorFromConfigs(
                source, "ACEScg", target, "Coverage Test", "Coverage", OCIO.TRANSFORM_DIR_FORWARD
            ).getDefaultCPUProcessor()
            checks.append(check(session, frame, processor, "Retained frame across working-space change"))
            reports.append(dict(device=device, checks=checks))
finally:
    if original_env is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_env)
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()

report = dict(
    result="passed",
    scope="RGB display conversion preserves renderer coverage alpha; fractional transparency remains a separate gate",
    devices=reports,
)
(destination / "houdini-display-alpha-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
