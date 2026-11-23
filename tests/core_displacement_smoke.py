# SPDX-License-Identifier: Apache-2.0
"""Independent positioned-mesh images and core source-lifetime checks."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
from pathlib import Path
import subprocess

import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/displacement"
reports = []
for device in ("CPU", ch.default_device()):
    prefix = "cpu" if device == "CPU" else "optix"
    output = destination / ("core-" + prefix)
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--displacement-test",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, (device, run.stdout, run.stderr)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert events[-1]["result"] == "passed" and events[-1]["checks"] == 10, events
    images = []
    for kind in ("plane", "seams"):
        native = OIIO.ImageBuf(str(destination / (prefix + ("-seams" if kind == "seams" else "") + ".exr")))
        reference = OIIO.ImageBuf(str(output / ("reference-" + kind + ".exr")))
        actual = native.get_pixels()
        expected = reference.get_pixels()
        assert actual.shape == expected.shape and actual.shape[:2] == (90, 160)
        maximum = float(np.max(np.abs(actual[:, :, :4] - expected[:, :, :4])))
        mean = float(np.mean(np.abs(actual[:, :, :4] - expected[:, :, :4])))
        assert maximum < 0.025 and mean < 0.001, (device, kind, maximum, mean)
        interior = (actual[:, :, 0] > 0.1999) & (expected[:, :, 0] > 0.1999)
        differences = {}
        for channel in ("depth.Z", "position.Z", "normal.Z"):
            index = list(native.spec().channelnames).index(channel)
            other = list(reference.spec().channelnames).index(channel)
            difference = float(np.max(np.abs(actual[:, :, index][interior] - expected[:, :, other][interior])))
            assert difference < 0.001, (device, kind, channel, difference)
            differences[channel] = difference
        images.append(
            dict(
                kind=kind,
                maximum_absolute_rgba_difference=maximum,
                mean_absolute_rgba_difference=mean,
                interior_data_differences=differences,
            )
        )
    reports.append(dict(device=device, result="passed", events=events, images=images, stderr=run.stderr))
(destination / "core-displacement-smoke.json").write_text(json.dumps(dict(result="passed", reports=reports), indent=2))
print(destination / "core-displacement-smoke.json")
