# SPDX-License-Identifier: Apache-2.0
"""Interrupt active heavy renders, preserve pause/Stop, and compare final accumulation."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import runpy
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/sampling-interruption"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-scene-interruption-smoke.json"
report = dict(
    result="running",
    device=device,
    checks=[],
    updates=[],
    scope="Actual million-triangle SOP render; active camera edits, pause and Stop/restart, final accumulated image versus a fresh session. No UI latency claim.",
)
# Build against the current HDA schema in an owned test directory. A saved
# artist/showcase HIP can predate ramp or parameter-layout changes.
folder = destination / "fixture"
build = runpy.run_path(str(root / "examples/sop-fx-courtyard/build_scene.py"), run_name="interruption_fixture_source")[
    "build"
]
build.__globals__["DESTINATION"] = folder
_, fixture_source = build()
report["fixture_source"] = fixture_source
hou.hipFile.load(fixture_source["example"], suppress_save_prompt=True)
hou.putenv("OCIO", str(folder / "showcase.ocio"))
hou.Color.reloadOCIO()
ch.synchronize_color_configuration()
hou.setFrame(1)
obj = hou.node("/obj/fx_courtyard")
obj.node("NOISE_DISPLACED_GROUND").parm("resolution").set(560)
obj.node("NOISE_DISPLACED_ROCKS").parm("count").set(80)
obj.node("ROCK_PROTOTYPE_SPHERE").setParms(dict(rows=36, cols=72))
obj.node("CURVE_VEGETATION_AND_CABLES").parm("count").set(600)
obj.node("SPARK_PARTICLES").parm("count").set(1200)
obj.node("INSTANCE_GRID").setParms(dict(rows=17, cols=21))
obj.node("STABLE_INSTANCE_POINTS").parm("snippet").set(
    "int x=int(rint((@P.x/8.64+.5)*20)); int z=int(rint((@P.z/6.24+.5)*16));\n"
    "if(abs(x-10)<2 && abs(z-8)<2) removepoint(0,@ptnum);\n"
    "i@id=1000+z*21+x; v@N={0,0,1}; v@up={0,1,0};"
)
report["adaptive_fixture"] = runpy.run_path(
    str(root / "examples/sop-fx-courtyard/adaptive_scene.py"), run_name="interrupt_fixture"
)["upgrade"]()
rop = hou.node("/out/cycles_sop_courtyard")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        resx=160,
        resy=90,
        samples=262144,
        viewer_interactive=0,
        adaptive_sampling=0,
        use_denoise=0,
        sampling_pattern="tabulated_sobol",
    )
)
camera = hou.node(rop.evalParm("camera"))
camera_x = camera.evalParm("tx")


def save():
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def until(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Heavy scene interruption stalled")
        time.sleep(0.005)


def first_image(session, target):
    def ready():
        status = session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        frame = session.frame_info()
        # DisplayDriver previews have no sample-count metadata. The session
        # reports accumulated samples; only full OutputDriver frames carry it.
        return frame and frame["generation"] == target and status["current_sample"] > 0

    until(ready)
    return session.status()


try:
    with ch.Session(
        device=device, width=160, height=90, samples=262144, execution="worker", collect_diagnostics=True
    ) as session:
        initial_scene = native_scene.capture(rop)
        first_image(session, session.apply(initial_scene))
        initial = session.status()
        pid = initial["renderer_pid"]
        owned = session._controller.acknowledged
        report["fixture"] = ch._native.snapshot_info(owned)
        assert report["fixture"]["mesh_triangles"] >= 1000000, report["fixture"]
        assert initial["subdivision_control_faces"] > 0
        for index in range(12):
            # Let the scheduler grow its sampling batch before requesting the
            # next camera. The last accepted frame must remain independently owned.
            time.sleep(0.12)
            before = session.status()
            previous = session.frame_info()
            assert before["backend_state"] == "rendering", before
            camera.parm("tx").set(camera_x + 0.025 * (index + 1))
            began = time.perf_counter()
            target = session.apply(dict(camera_path=camera.path()))
            after = first_image(session, target)
            assert after["renderer_pid"] == pid and after["backend_epoch"] == initial["backend_epoch"]
            assert after["geometry_conversions"] == initial["geometry_conversions"]
            assert previous["generation"] < target and session.frame_info()["generation"] == target
            diagnostic = session.diagnostics()
            assert diagnostic["session_cancel_calls"] == 0, diagnostic
            report["updates"].append(
                dict(
                    index=index,
                    generation=target,
                    host_to_first_frame_seconds=time.perf_counter() - began,
                    scene_boundary_wait_seconds=diagnostic["scene_boundary_wait_seconds"],
                )
            )
            save()
        report["checks"].append(
            "Twelve active camera edits retain worker and source geometry with no Session cancellation"
        )

        # A matching viewer attachment is a logical submission, but it must
        # neither interrupt physical sampling nor reset its accumulation.
        before = session.status()
        old_samples = before["current_sample"]
        target = session.apply(dict(camera_path=camera.path()), reuse_if_unchanged=True)
        first_image(session, target)
        # Owned no-op navigation can acknowledge without entering the engine.
        # Sampling continuity and physical identity, rather than an engine call
        # counter, establish that accumulation was retained.
        assert session.status()["render_revision"] == before["render_revision"]
        assert session.status()["geometry_conversions"] == before["geometry_conversions"]
        assert session.status()["renderer_pid"] == pid
        until(lambda: session.status()["current_sample"] > old_samples)
        assert session.diagnostics()["session_cancel_calls"] == 0
        report["checks"].append("Unchanged active submission retains accumulation and continues sampling")

        session.pause(True)
        until(lambda: session.status()["state"] == "paused")
        old_frame = session.frame_info()
        camera.parm("tx").set(camera_x + 0.4)
        target = session.apply(dict(camera_path=camera.path()))
        until(lambda: session.status()["committed_generation"] == target and not session.status()["pending_update"])
        assert session.status()["state"] == "paused"
        time.sleep(0.15)
        assert session.frame_info()["generation"] == old_frame["generation"]
        session.pause(False)
        first_image(session, target)
        assert session.diagnostics()["session_cancel_calls"] == 0
        report["checks"].append("An update while paused remains paused and resumes the new scene")

        session.cancel()
        until(lambda: session.status()["state"] == "cancelled")
        assert session.diagnostics()["session_cancel_calls"] == 1
        camera.parm("tx").set(camera_x + 0.45)
        # RenderSettings is a complete authored record, not a sparse patch.
        # Preserve film/filter/path settings for the matched fresh reference.
        final_settings = dict(initial_scene["rendering"], samples=16)
        target = session.apply(dict(camera_path=camera.path(), rendering=final_settings))
        completed = session.wait(target, timeout=120)
        assert completed["complete"] and completed["samples"] == 16
        actual = np.frombuffer(completed["planes"]["combined"]["pixels"], np.float32).copy()
        assert np.isfinite(actual).all() and actual.std() > 0.02
        assert session.status()["renderer_pid"] == pid
        report["checks"].append(
            "Explicit Stop is the sole cancellation and the retained worker completes a restarted render"
        )
        report["final"] = session.status()
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0

    # A fresh ordered render must reach the same accumulation, rather than
    # inheriting a partial/cancelled old batch or a late interrupt of the new one.
    rop.parm("samples").set(16)
    with ch.Session(device=device, width=160, height=90, samples=16, interactive=False, execution="sync") as reference:
        fresh = reference.wait(reference.apply(native_scene.capture(rop)), timeout=120)
        expected = np.frombuffer(fresh["planes"]["combined"]["pixels"], np.float32)
        rmse = float(np.sqrt(np.mean((actual - expected) ** 2)))
        scale = max(float(np.sqrt(np.mean(expected**2))), 0.01)
        settings_equal = reference.status()["rendering"] == report["final"]["rendering"]
        report["comparison"] = dict(
            rmse=rmse,
            relative_rmse=rmse / scale,
            reference_samples=fresh["samples"],
            render_settings_equal=settings_equal,
            resolved_sampling_pattern="tabulated_sobol",
        )
        np.save(destination / "interruption-actual.npy", actual)
        np.save(destination / "interruption-reference.npy", expected)
        assert settings_equal, (reference.status()["rendering"], report["final"]["rendering"])
        assert rmse / scale < 0.01, (rmse, scale)
    report["checks"].append("Final full-resolution accumulation matches an independently restarted render")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    save()
    print(json.dumps(dict(result=report["result"], report=str(report_path))))
