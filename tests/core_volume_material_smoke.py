# SPDX-License-Identifier: Apache-2.0
"""Independent volume inputs verify native grid/graph conversion images."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json, os, subprocess
from pathlib import Path
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/volumes"
reports = []
for device in ("CPU", ch.default_device()):
    label = "cpu" if device == "CPU" else "optix"
    output = destination / ("core-" + label)
    run = subprocess.run(
        [
            str(runtime_executable("ch_harness.exe")),
            "--resources",
            os.environ["CYCLES_HOUDINI_RESOURCES"],
            "--device",
            device,
            "--volume-material-test",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert events[-1]["result"] == "passed" and events[-1]["checks"] == 15, events
    images = []
    for kind in ("absorption", "emission", "blackbody"):
        native = OIIO.ImageBuf(str(destination / (label + "-" + kind + ".exr")))
        reference = OIIO.ImageBuf(str(output / ("reference-" + kind + ".exr")))
        actual = native.get_pixels()[:, :, :4]
        expected = reference.get_pixels()[:, :, :4]
        working_space = ch.color_policy()["working_space"]
        assert (
            native.spec().get_string_attribute("oiio:ColorSpace")
            == reference.spec().get_string_attribute("oiio:ColorSpace")
            == working_space
        )
        assert actual.shape == expected.shape == (48, 80, 4)
        maximum = float(np.max(np.abs(actual - expected)))
        # Explicit emission and convenience Principled emission use different
        # equivalent graphs; sampling/image comparisons remain statistical.
        tolerance = 0.025 if kind != "blackbody" else 0.1
        assert maximum < tolerance, (device, kind, maximum, tolerance)
        images.append(
            dict(
                kind=kind,
                maximum_absolute_rgba_difference=maximum,
                mean_absolute_rgba_difference=float(np.mean(np.abs(actual - expected))),
                tolerance=tolerance,
                working_space=working_space,
            )
        )
    reports.append(dict(device=device, result="passed", events=events, images=images, stderr=run.stderr))
(destination / "core-volume-material-smoke.json").write_text(
    json.dumps(dict(result="passed", reports=reports), indent=2), encoding="utf-8"
)
print(json.dumps(dict(result="passed", images_per_device=3)))
