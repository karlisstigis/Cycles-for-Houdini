# SPDX-License-Identifier: Apache-2.0
"""Real multi-tile final buffers preserve AOVs, denoising, crops and worker delivery."""

from support.paths import artifacts_root, configure_test_environment
from native_test_runtime import runtime_context
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

# Native trace proves that Cycles actually spills six/four tiles rather than
# accepting a flag while silently rendering a single full-frame buffer.
os.environ["CYCLES_HOUDINI_TRACE_KERNELS"] = "1"
temporary = configure_test_environment()

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene
from cycles_houdini.device_selection import set_devices


destination = artifacts_root() / "stage3/final-tiling"
destination.mkdir(parents=True, exist_ok=True)
parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path, default=destination / "houdini-final-tiling-smoke.json")
parser.add_argument("--render-child", action="store_true", help=argparse.SUPPRESS)
args = parser.parse_args()
device = args.device
if not args.render_child:
    run = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--device",
            device,
            "--report",
            str(args.report),
            "--render-child",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    (args.report.parent / "tiling.stdout.log").write_text(run.stdout, encoding="utf-8")
    (args.report.parent / "tiling.stderr.log").write_text(run.stderr, encoding="utf-8")
    assert run.returncode == 0, run.stderr[-16000:]
    actual_tiles = run.stderr.count("Write tile result to disk.")
    assert actual_tiles >= 21, (actual_tiles, "Expected 20 matrix tiles plus at least one cancelled spilled tile")
    assert run.stderr.count("Processing full frame buffer file") == 4, "Missing full-frame reassembly/denoising"
    report = json.loads(args.report.read_text(encoding="utf-8"))
    report.update(actual_sync_tiles=actual_tiles, full_frame_reloads=4)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    raise SystemExit(0)
subject = hou.node("/obj").createNode("geo", "tiling_subject", run_init_scripts=False)
box = subject.createNode("box")
camera = hou.node("/obj").createNode("cam", "tiling_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
material = dict(
    key="tiling_surface",
    kind="graph",
    graph=dict(
        nodes=[
            dict(id="emission", definition="cycles/emission/1", parameters=dict(color=[0.2, 0.4, 0.8])),
            dict(id="tint", definition="cycles/aov_output/1", parameters=dict(name="TileTint", color=[0.3, 0.7, 1.2])),
            dict(id="mask", definition="cycles/aov_output/1", parameters=dict(name="TileMask", value=0.37)),
        ],
        links=[],
        surface=dict(node="emission", socket="emission"),
    ),
)
base = dict(
    geometry=[ch.geometry_request(subject, box, material="tiling_surface")],
    camera_path=camera.path(),
    background=(0.025, 0.05, 0.1),
    materials=[material],
    aovs=[dict(name="TileTint", type="color"), dict(name="TileMask", type="value")],
    rendering=dict(samples=8, adaptive_sampling=False, seed=9),
)
checks = []
reference = {}
initial_directories = set(temporary.glob("cycles-houdini-tiles-*"))


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


for execution in ("sync", "worker"):
    for denoise in (False, True):
        for tiled in (False, True):
            label = f"{execution}-{'oidn' if denoise else 'raw'}-{'tiled' if tiled else 'whole'}"
            print("TILING_CASE " + label, flush=True)
            with ch.Session(
                device=device,
                width=96,
                height=64,
                samples=8,
                interactive=False,
                execution=execution,
                use_tiling=tiled,
                tile_size=32,
                threads=2,
                passes=["combined", "depth", "normal", "position", "sample_count"],
                denoising=dict(enabled=denoise, method="oidn", use_gpu=False),
                collect_diagnostics=True,
            ) as session:
                for cropped in (False, True):
                    update = dict(base) if not cropped else dict(crop=(11, 7, 57, 39))
                    frame = session.wait(session.apply(update), timeout=180)
                    size = (57, 39) if cropped else (96, 64)
                    assert frame["complete"] and (frame["width"], frame["height"]) == size, frame
                    assert (frame["full_width"], frame["full_height"]) == (96, 64), frame
                    assert {"TileTint", "TileMask", "combined", "normal", "depth", "sample_count"} <= frame[
                        "planes"
                    ].keys()
                    assert np.isclose(plane(frame, "TileMask").max(), 0.37, atol=1e-6)
                    assert np.isclose(plane(frame, "TileTint")[:, :, 2].max(), 1.2, atol=1e-6)
                    assert plane(frame, "sample_count").max() == 8
                    key = (denoise, cropped)
                    if key not in reference:
                        reference[key] = {name: plane(frame, name).copy() for name in frame["planes"]}
                    errors = {}
                    for name, expected in reference[key].items():
                        actual = plane(frame, name)
                        errors[name] = float(np.abs(actual - expected).max())
                        np.testing.assert_allclose(actual, expected, atol=2e-5, rtol=2e-5, err_msg=label + ":" + name)
                    path = destination / (label + ("-crop" if cropped else "") + ".exr")
                    session.write_exr(path)
                    image = oiio.ImageInput.open(str(path))
                    assert image, oiio.geterror()
                    spec = image.spec()
                    expected_window = (11, 18, 57, 39, 96, 64) if cropped else (0, 0, 96, 64, 96, 64)
                    assert (
                        spec.x,
                        spec.y,
                        spec.width,
                        spec.height,
                        spec.full_width,
                        spec.full_height,
                    ) == expected_window
                    assert {"TileTint.R", "TileMask.X", "depth.Z"} <= set(spec.channelnames), spec.channelnames
                    assert np.isfinite(image.read_image(format=oiio.FLOAT)).all()
                    image.close()
                    checks.append(dict(case=label, cropped=cropped, size=size, max_errors=errors))
            assert set(temporary.glob("cycles-houdini-tiles-*")) == initial_directories, (
                "Completed session leaked tile files"
            )


# Explicit IPR requests ignore tiling, including in worker configuration capture.
def session_tiling(config, *, interactive):
    from cycles_houdini.ipr import registry

    with patch.object(registry, "Session") as create_session:
        registry.session_from_configuration(config, interactive=interactive)
    create_session.assert_called_once()
    options = create_session.call_args.kwargs
    return options["use_tiling"], options["tile_size"]


rop = hou.node("/out").createNode("cycles_ipr", "tiling_configuration")
set_devices(rop, device)
rop.setParms(dict(use_tiling=1, tile_size=32))
ipr_config = native_scene.configuration(rop, interactive=True)
assert session_tiling(ipr_config, interactive=True) == (False, 2048)
rop.setParms(dict(use_tiling=0, tile_size=17))
assert native_scene.configuration(rop, interactive=True) == ipr_config
rop.setParms(dict(use_tiling=1, tile_size=32))
final_config = native_scene.configuration(rop, interactive=False)
assert session_tiling(final_config, interactive=False) == (True, 32)
with ch.Session(
    device=device, width=96, height=64, samples=2, interactive=True, execution="sync", use_tiling=True, tile_size=1
) as session:
    assert session.wait(session.calibration())["width"] == 96

# Cancel after at least one completed tile has reached the temporary EXR.
with ch.Session(
    device=device,
    width=160,
    height=96,
    samples=4096,
    interactive=False,
    execution="sync",
    use_tiling=True,
    tile_size=8,
    threads=2,
) as session:
    session.calibration()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not list(temporary.glob("cycles-houdini-tiles-*/*.exr")):
        assert session.status()["state"] != "failed", session.status()
        time.sleep(0.01)
    assert list(temporary.glob("cycles-houdini-tiles-*/*.exr")), "Cancellation test never produced a real spilled tile"
    session.cancel()
    assert session.status()["state"] == "cancelled"
assert set(temporary.glob("cycles-houdini-tiles-*")) == initial_directories, "Cancelled session leaked tile files"

report = dict(
    result="passed",
    runtime=runtime_context(),
    device=device,
    cases=checks,
    crop="preserved",
    aovs="preserved",
    oidn="whole-frame",
    ipr="unchanged",
    temporary_files="retired after completion and cancellation",
    expected_sync_tiles=20,
)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report), flush=True)
