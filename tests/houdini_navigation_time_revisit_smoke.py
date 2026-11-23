# SPDX-License-Identifier: Apache-2.0
"""A fresh scene supersedes older poses when scrubbing back to their time."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import sys
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/navigation-time"
destination.mkdir(parents=True, exist_ok=True)
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
hou.setFps(24)
hou.setFrame(10)
geo = hou.node("/obj").createNode("geo", "revisit_subject", run_init_scripts=False)
box = geo.createNode("box")
box.parmTuple("size").set((1, 1, 0.1))
camera = hou.node("/obj").createNode("cam", "revisit_camera")
pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "revisit_render")
set_rop_devices(rop, device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="sop",
        soppath=box.path(),
        camera=camera.path(),
        resx=128,
        resy=96,
        samples=16,
        ipr_samples=16,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        pass_motion=1,
        material_kind="emission",
    ),
)
checks = []


def render(session, label, expected):
    frame = session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
    p = frame["planes"]["motion"]
    actual = np.median(np.frombuffer(p["pixels"], np.float32).reshape(96, 128, 4)[40:56, 56:72], axis=(0, 1))
    assert np.allclose(actual, expected, atol=0.025), (label, actual, expected)
    status = session.status()
    assert not status.get("pending_update") and not status.get("capture_pending")
    checks.append(dict(label=label, frame=hou.frame(), generation=frame["generation"], motion=actual.tolist()))
    return frame


with ch.Session(
    device=device, width=128, height=96, samples=16, interactive=True, execution="worker", collect_diagnostics=True
) as session:
    render(session, "static", [0, 0, 0, 0])
    pass_fixture.parm(geo, "tx").setExpression("($FF-10)*0.1")
    render(session, "moving object", [-3.2, 0, -3.2, 0])
    rop.parmTuple("background").set((0.3, 0.4, 0.5))
    render(session, "same-time navigation retained", [-3.2, 0, -3.2, 0])
    for number in range(8, 13):
        hou.setFrame(number)
        render(session, "scrub " + str(number), [-3.2, 0, -3.2, 0])
    hou.setFrame(10)
    pass_fixture.parm(geo, "tx").deleteAllKeyframes()
    pass_fixture.parm(geo, "tx").set(0)
    pass_fixture.parm(camera, "tx").setExpression("($FF-10)*0.1")
    current = render(session, "return with new camera animation", [3.2, 0, 3.2, 0])
    assert session.diagnostics()["session_cancel_calls"] == 0
    worker_pid = session.status()["renderer_pid"]

# Independent requested-frame capture; do not replay the worker's own snapshot.
with ch.Session(device=device, width=128, height=96, samples=16, interactive=True, execution="sync") as session:
    reference = render(session, "independent fresh scene", [3.2, 0, 3.2, 0])
    errors = {}
    for name in ("combined", "motion"):
        actual = np.frombuffer(current["planes"][name]["pixels"], np.float32)
        expected = np.frombuffer(reference["planes"][name]["pixels"], np.float32)
        errors[name] = float(np.abs(actual - expected).max())
        assert np.allclose(actual, expected, atol=2e-5, rtol=2e-5), (name, errors[name])

pass_fixture.parm(camera, "tx").deleteAllKeyframes()
pass_fixture.parm(camera, "tx").set(0)
pass_fixture.parm(rop, "pass_depth").set(1)
with ch.Session(device=device, width=128, height=96, samples=16, interactive=True, execution="worker") as session:

    def bounds(label, request, expected):
        frame = session.wait(session.apply(request, at_time=hou.time()))
        depth = np.frombuffer(frame["planes"]["depth"]["pixels"], np.float32).reshape(96, 128)
        ys, xs = np.where((depth > 0) & (depth < 100))
        actual = [int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())]
        assert actual == expected, (label, actual, expected)
        checks.append(dict(label=label, bounds=actual, generation=frame["generation"]))

    bounds("partial baseline", native_scene.capture(rop), [48, 79, 32, 63])
    pass_fixture.parm(camera, "tx").set(0.5)
    motion = native_scene.motion_configuration(rop, hou.time())
    bounds("camera-only delta", dict(camera_path=camera.path(), motion=motion), [32, 63, 32, 63])
    before = session.status()["geometry_conversions"]
    bounds("background delta retains camera", dict(background=(0.8, 0.4, 0.2), motion=motion), [32, 63, 32, 63])
    assert session.status()["geometry_conversions"] == before
    pass_fixture.parm(geo, "tx").set(0.25)
    request = native_scene.capture(rop)
    bounds("geometry delta retains camera", dict(geometry=request["geometry"], motion=motion), [40, 71, 32, 63])

report = dict(
    result="passed",
    device=device,
    checks=checks,
    independent_max_errors=errors,
    worker_pid=worker_pid,
    ordinary_cancel_calls=0,
)
(destination / "houdini-navigation-time-revisit-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
