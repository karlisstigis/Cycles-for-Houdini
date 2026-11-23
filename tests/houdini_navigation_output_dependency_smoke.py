# SPDX-License-Identifier: Apache-2.0
"""New-pass controls wait for complete geometry, while camera/Pause/Stop bypass it."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import threading
import time
import hou
import cycles_houdini as ch
from cycles_houdini import _native

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
obj = hou.node("/obj").createNode("geo", "navigation_pass_dependency", run_init_scripts=False)
box = obj.createNode("box")
deform = obj.createNode("xform")
deform.setInput(0, box)
deform.setDisplayFlag(True)
deform.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "navigation_pass_camera")
camera.parm("tz").set(5)
motion = dict(enabled=False, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3)


def scene(passes):
    return dict(
        geometry=[ch.geometry_request(obj, deform, material="surface", material_mode="uniform")],
        materials=[dict(key="surface", kind="emission", color=(0.2, 0.6, 0.1), strength=1)],
        camera_path=camera.path(),
        motion=motion,
        passes=passes,
    )


def until(predicate, timeout=8):
    began = time.monotonic()
    while not predicate():
        assert time.monotonic() - began < timeout, "Timed out waiting for the worker dependency"
        time.sleep(0.005)


original = _native.convert_apply
checks = []
with ch.Session(
    device=device, width=240, height=160, samples=32, interactive=True, execution="worker", collect_diagnostics=True
) as session:
    session.wait(session.apply(scene(["combined"])))
    pid = session.status()["renderer_pid"]
    unavailable = session._controller.control("preview", "normal")
    until(lambda: (session.status(), unavailable.is_set())[1])
    assert "not enabled" in unavailable.error
    assert session.status()["state"] != "failed" and session.status()["renderer_pid"] == pid
    assert session.diagnostics()["controller"]["control_rejections"][-1]["command"] == "preview"
    checks.append("A validly formed but unavailable display pass is rejected without stopping the renderer")
    for index, plane in enumerate(("normal", "position")):
        entered = threading.Event()
        release = threading.Event()

        def held(job):
            entered.set()
            assert release.wait(8)
            return original(job)

        try:
            _native.convert_apply = held
            deform.parm("rx").set(15 * (index + 1))
            source = session.apply(scene(["combined", plane]))
            until(lambda: (session.status(), entered.is_set())[1])
            camera.parm("tx").set(0.2 * (index + 1))
            current = session.apply(dict(camera_path=camera.path(), motion=motion))
            until(
                lambda: (
                    session.status(),
                    (f := session.frame_info()) and f["generation"] == current and f.get("current_scene"),
                )[1]
            )
            assert session.status()["committed_scene_submission"] < source
            pending = session._controller.control("preview", plane)
            began = time.monotonic()
            while time.monotonic() - began < 0.1:
                assert not pending.is_set()
                assert session.status()["state"] != "failed"
                time.sleep(0.005)
            checks.append("Camera preview does not prematurely release the pending " + plane + " control")
            if index == 0:
                release.set()
                _native.convert_apply = original
                until(lambda: (session.status(), pending.is_set())[1])
                assert not pending.error and session.status()["committed_scene_submission"] >= source
                frame = session.wait(current)
                assert plane in frame["planes"] and session.status()["renderer_pid"] == pid
                checks.append("New normal pass becomes selectable only after its actual scene commits")
            else:
                session.pause(True)
                until(lambda: session.status()["backend_state"] == "paused")
                assert not pending.is_set()
                session.cancel()
                until(lambda: session.status()["state"] == "cancelled")
                assert pending.is_set() and session.status()["renderer_pid"] == pid
                checks.append("Pause and Stop bypass a future-pass preference; Stop retires the preference safely")
        finally:
            release.set()
            _native.convert_apply = original
    session.pause(False)
    session.wait(session.apply(scene(["combined"])))
    assert session.status()["renderer_pid"] == pid
    checks.append("Stopped conversion and pass changes restart in the same worker")
with ch.Session(device=device, width=240, height=160, samples=32, interactive=True, execution="worker") as initial:
    initial.set_preview_pass("normal")
    initial.wait(initial.apply(scene(["combined"])))
    assert initial.status()["state"] == "complete"
    assert initial.diagnostics()["controller"]["control_rejections"][-1]["command"] == "preview"
    checks.append("An unavailable initial preview preference retains the first usable render")
report = dict(
    result="passed",
    device=device,
    checks=checks,
    scope="Real worker/camera images and deterministic host conversion barriers; pass dependencies use complete source commits, not early preview generations.",
)
dest = artifacts_root() / "stage1/navigation"
dest.mkdir(parents=True, exist_ok=True)
(dest / "houdini-navigation-output-dependency-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks), device=device)))
