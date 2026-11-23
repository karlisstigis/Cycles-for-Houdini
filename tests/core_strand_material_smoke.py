# SPDX-License-Identifier: Apache-2.0
"""Matched engine palette validation and independently constructed images."""

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
destination = artifacts_root() / "stage3/materials/strands"
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
            "--strand-material-test",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, (device, run.stdout, run.stderr)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert events[-1]["result"] == "passed" and events[-1]["checks"] == 30, events
    images = []
    for kind in ("points", "curves"):
        native = OIIO.ImageBuf(str(destination / (prefix + "-" + kind + ".exr")))
        reference = OIIO.ImageBuf(str(output / ("reference-" + kind + ".exr")))
        actual = native.get_pixels()[:, :, :4]
        expected = reference.get_pixels()[:, :, :4]
        assert actual.shape == expected.shape == (96, 96, 4)
        maximum = float(np.max(np.abs(actual - expected)))
        assert maximum < 0.001, (device, kind, maximum)
        images.append(
            dict(
                kind=kind,
                maximum_absolute_rgba_difference=maximum,
                native=str(destination / (prefix + "-" + kind + ".exr")),
                reference=str(output / ("reference-" + kind + ".exr")),
            )
        )
    reports.append(dict(device=device, result="passed", events=events, images=images, stderr=run.stderr))
(destination / "core-strand-material-smoke.json").write_text(
    json.dumps(dict(result="passed", reports=reports), indent=2), encoding="utf-8"
)
print(destination / "core-strand-material-smoke.json")
