# SPDX-License-Identifier: Apache-2.0
"""Compare native data with an independently authored matched engine scene."""

from native_test_runtime import runtime_executable
from array import array
import os
from pathlib import Path
import subprocess

import OpenImageIO as OIIO


def compare_reference(frame, device, fixture, destination):
    root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
    path = destination / (fixture + "-reference-" + ("cpu" if device == "CPU" else "optix") + ".exr")
    subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--fixture",
            fixture,
            "--width",
            str(frame["width"]),
            "--height",
            str(frame["height"]),
            "--samples",
            "1",
            "--output",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    image = OIIO.ImageInput.open(str(path))
    assert image, OIIO.geterror()
    try:
        spec = image.spec()
        pixels = image.read_image(format=OIIO.FLOAT)
        channels = {name: index for index, name in enumerate(spec.channelnames)}
        errors = {}
        for name, suffixes in (
            ("combined", ("R", "G", "B")),
            ("position", ("X", "Y", "Z")),
            ("normal", ("X", "Y", "Z")),
            ("depth", ("Z",)),
            ("uv", ("U", "V")),
        ):
            native = array("f")
            native.frombytes(frame["planes"][name]["pixels"])
            count = frame["planes"][name]["channels"]
            maximum = 0.0
            for world_x in (-0.8, 0.8):
                x = round((world_x / 4 + 0.5) * frame["width"])
                y = frame["height"] // 2
                index = y * frame["width"] + x
                for component, suffix in enumerate(suffixes):
                    channel_name = ("" if name == "combined" else name + ".") + suffix
                    expected = float(pixels[spec.height - 1 - y, x, channels[channel_name]])
                    maximum = max(maximum, abs(expected - native[index * count + component]))
            assert maximum < 0.002, (fixture, device, name, maximum, spec.channelnames)
            errors[name] = maximum
        return dict(
            file=str(path),
            maximum_interior_absolute_errors=errors,
            provenance="Harness fixture is authored independently of the native adapter",
        )
    finally:
        image.close()
