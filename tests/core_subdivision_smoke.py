# SPDX-License-Identifier: Apache-2.0
"""Independent Catmull-Clark equations and core lifecycle/allocation evidence."""

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
destination = artifacts_root() / "stage2/subdivision"
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
            "--subdivision-test",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, (device, run.stdout, run.stderr)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert events[-1]["result"] == "passed" and events[-1]["checks"] == 9, events
    native = OIIO.ImageBuf(str(destination / (prefix + "-cube.exr")))
    reference = OIIO.ImageBuf(str(output / "reference-cube.exr"))
    actual = native.get_pixels()
    expected = reference.get_pixels()
    assert actual.shape == expected.shape and actual.shape[:2] == (90, 160)
    maximum = float(np.max(np.abs(actual[:, :, :4] - expected[:, :, :4])))
    mean = float(np.mean(np.abs(actual[:, :, :4] - expected[:, :, :4])))
    # Native limit patches versus an independently refined level-five polygon
    # reference: allow narrow silhouette differences; compare interior data too.
    assert maximum <= 0.1 + 1e-6 and mean < 0.002, (device, maximum, mean)
    interior = (actual[:, :, 0] > 0.1999) & (expected[:, :, 0] > 0.1999)
    differences = {}
    for channel, tolerance in (("depth.Z", 0.01), ("position.Z", 0.01), ("normal.Z", 0.08)):
        index = list(native.spec().channelnames).index(channel)
        other = list(reference.spec().channelnames).index(channel)
        difference = float(np.max(np.abs(actual[:, :, index][interior] - expected[:, :, other][interior])))
        assert difference < tolerance, (device, channel, difference, tolerance)
        differences[channel] = dict(maximum_absolute_difference=difference, tolerance=tolerance)
    reports.append(
        dict(
            device=device,
            result="passed",
            events=events,
            maximum_absolute_rgba_difference=maximum,
            mean_absolute_rgba_difference=mean,
            interior_data=differences,
            stderr=run.stderr,
        )
    )
(destination / "core-subdivision-smoke.json").write_text(json.dumps(dict(result="passed", reports=reports), indent=2))
print(destination / "core-subdivision-smoke.json")
