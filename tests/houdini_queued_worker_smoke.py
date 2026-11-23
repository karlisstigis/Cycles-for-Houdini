# SPDX-License-Identifier: Apache-2.0
"""Transport, ownership and coalescing correctness on the real SOP courtyard.

This is a structural regression, not the GUI response benchmark. Existing
independent feature references validate the Houdini-to-Cycles conversion.
"""

from support.paths import artifacts_root
from native_test_runtime import build_manifest
import pass_fixtures
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
example = artifacts_root() / "examples/sop-fx-courtyard"
destination = artifacts_root() / "stage1/queued-workers" / (args.device.replace(" ", "_").replace(":", "_"))
destination.mkdir(parents=True, exist_ok=True)
report_path = artifacts_root() / "stage1/queued-workers/houdini-queued-worker-smoke.json"
report = dict(
    result="running",
    device=args.device,
    cases=[],
    images={},
    scope="Courtyard transport/owned state/coalescing/cancel/recovery; GUI timings measured separately",
)
(destination / "build-manifest.json").write_bytes((build_manifest()).read_bytes())
hou.hipFile.load(str(example / "sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True)
hou.putenv("OCIO", str(example / "showcase.ocio"))
hou.Color.reloadOCIO()
rop = hou.node("/out/cycles_sop_courtyard")
camera = hou.node("/obj/courtyard_camera")
hero = hou.node("/obj/fx_courtyard/TWISTED_TURBINE")
height = hero.parm("height").eval()
camera_x = camera.parm("tx").eval()
# A few high-energy pixel differences in independent GPU renders dominated a
# 16-sample courtyard comparison. Keep the existing
# beauty tolerance, increase convergence, and separately verify raw geometry.
comparison_samples = 128
set_rop_devices(rop, args.device)
rop.setParms(dict(resx=320, resy=180, samples=comparison_samples))
pass_fixtures.set_parameters(rop, dict(pass_depth=1, pass_position=1))
report["comparison_samples"] = comparison_samples


def until(predicate, timeout=20.0):
    started = time.monotonic()
    while not predicate():
        if time.monotonic() - started > timeout:
            raise TimeoutError("Queued renderer condition did not complete")
        time.sleep(0.01)


def image(session, generation):
    frame = session.wait(generation, timeout=40.0)
    result = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    assert np.isfinite(result).all() and result.std() > 0.05
    return result


def save():
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


baseline = None
baseline_data = None
for mode in ("sync", "worker"):
    hero.parm("height").set(height)
    camera.parm("tx").set(camera_x)
    session = ch.Session(
        device=args.device, width=320, height=180, samples=comparison_samples, collect_diagnostics=True, execution=mode
    )
    try:
        generation = session.apply(native_scene.capture(rop))
        pixels = image(session, generation)
        frame = session.frame()
        geometry_data = {
            name: np.frombuffer(frame["planes"][name]["pixels"], np.float32).copy() for name in ("depth", "position")
        }
        status = session.status()
        assert status["geometry_resources"] == 5 and status["packed_instances"] == 54, status
        assert status["points"] == 360 and status["curves"] == 265, status
        assert status["displacement_rebuilds"] == 1, status
        session.write_exr(destination / (mode + ".exr"))
        report["images"][mode] = dict(
            sha256=hashlib.sha256(pixels.tobytes()).hexdigest(), status=status, diagnostics=session.diagnostics()
        )
        if baseline is None:
            baseline = pixels
            baseline_data = geometry_data
        else:
            rmse = float(np.sqrt(np.mean((pixels - baseline) ** 2)))
            report["images"][mode]["baseline_rmse"] = rmse
            report["images"][mode]["data_pass_max_errors"] = {
                name: float(np.max(np.abs(values - baseline_data[name]))) for name, values in geometry_data.items()
            }
            save()
            for name, values in geometry_data.items():
                assert np.allclose(values, baseline_data[name], rtol=1e-6, atol=1e-5), name
            assert rmse < 0.005, (mode, rmse)
        if mode == "sync":
            continue
        assert status["renderer_pid"] != os.getpid()
        assert session.diagnostics()["session_cancel_calls"] == 0

        # Apply a camera edit while paused: it must ACK without full cancellation
        # or deadlocking the render-loop's wait-for-work state.
        session.pause(True)
        until(lambda: session.status()["state"] == "paused")
        camera.parm("tx").set(camera_x + 0.02)
        target = session.apply(native_scene.capture(rop))
        until(lambda: session.status()["committed_generation"] == target)
        assert session.status()["state"] == "paused", session.status()
        session.pause(False)
        image(session, target)

        # Hold the engine owner, not Houdini. Deterministically coalesce two
        # pending captures while retaining the earlier geometry edit.
        entered, release = threading.Event(), threading.Event()
        backend = session._controller.backend
        original = backend.apply

        def held(*values):
            entered.set()
            if not release.wait(10.0):
                raise TimeoutError("Test acknowledgement barrier timed out")
            return original(*values)

        backend.apply = held
        before = session.status()
        hero.parm("height").set(height + 0.15)
        first = session.apply(native_scene.capture(rop))
        until(lambda: (session.status(), entered.is_set())[1], timeout=10.0)
        camera.parm("tx").set(camera_x + 0.05)
        second = session.apply(native_scene.capture(rop))
        camera.parm("tx").set(camera_x + 0.09)
        final = session.apply(native_scene.capture(rop))
        assert final > second > first
        release.set()
        edited = image(session, final)
        backend.apply = original
        after = session.status()
        assert after["coalesced_updates"] >= 1 and after["geometry_conversions"] > before["geometry_conversions"], after
        assert after["geometry_resources"] == before["geometry_resources"] and after["points"] == 360, after
        with ch.Session(device=args.device, width=320, height=180, samples=16, execution="sync") as reference:
            expected = image(reference, reference.apply(native_scene.capture(rop)))
        rmse = float(np.sqrt(np.mean((edited - expected) ** 2)))
        assert rmse < 0.005, (mode, "coalesced geometry/camera mismatch", rmse)
        assert session.diagnostics()["session_cancel_calls"] == 0
        report["cases"].append(
            dict(
                mode=mode,
                name="paused update and lossless coalescing",
                rmse=rmse,
                before=before,
                after=after,
                diagnostics=session.diagnostics(),
            )
        )
        if mode == "worker":
            renderer_pid = session.status()["renderer_pid"]
            backend.process.kill()
            until(lambda: session.status()["state"] == "failed")
            failure = session.status()
            assert failure["error"] and session.frame_info() is not None
            recovered = session.apply(native_scene.capture(rop))
            image(session, recovered)
            status = session.status()
            assert status["renderer_pid"] != renderer_pid and status["backend_epoch"] == 2
            assert status["geometry_resources"] == 5 and status["object_resources"] == after["object_resources"], status
            report["cases"].append(
                dict(mode=mode, name="forced renderer termination and recovery", failure=failure, recovered=status)
            )
        session.pause(True)
        until(lambda: session.status()["state"] == "paused")
        before_cancel = time.perf_counter()
        session.cancel()
        cancel_submission_seconds = time.perf_counter() - before_cancel
        until(lambda: session.status()["state"] == "cancelled")
        restarted = session.apply(native_scene.capture(rop))
        image(session, restarted)
        report["cases"].append(
            dict(
                mode=mode,
                name="explicit cancel while paused and restart",
                cancel_submission_seconds=cancel_submission_seconds,
                diagnostics=session.diagnostics(),
            )
        )

        # A normal Stop -> Render action must not expose the previous render's
        # cancelled state as the new generation. Hold the next engine apply
        # after Stop has been acknowledged to make that race deterministic.
        entered, release = threading.Event(), threading.Event()
        backend = session._controller.backend
        original = backend.apply

        def held_restart(*values):
            entered.set()
            if not release.wait(10.0):
                raise TimeoutError("Immediate restart barrier timed out")
            return original(*values)

        backend.apply = held_restart
        try:
            before = session.status()
            session.cancel()
            restarted = session.apply(native_scene.capture(rop))
            until(lambda: (session.status(), entered.is_set())[1], timeout=10.0)
            pending = session.status()
            assert pending["generation"] == restarted and pending["committed_generation"] < restarted, pending
            assert pending["state"] == "rendering" and pending["pending_update"], pending
            release.set()
            image(session, restarted)
            assert session.status()["geometry_conversions"] == before["geometry_conversions"]
            report["cases"].append(
                dict(
                    mode=mode,
                    name="immediate Stop -> Render retains the new generation",
                    pending=pending,
                    after=session.status(),
                )
            )
        finally:
            release.set()
            backend.apply = original

        # Stop can also discard a newer edit while an older apply is in flight.
        # A discarded submission must not remain marked as a pending update.
        entered, release = threading.Event(), threading.Event()
        backend.apply = held_restart
        try:
            camera.parm("tx").set(camera_x + 0.11)
            inflight = session.apply(native_scene.capture(rop))
            until(lambda: (session.status(), entered.is_set())[1], timeout=10.0)
            camera.parm("tx").set(camera_x + 0.13)
            discarded = session.apply(native_scene.capture(rop))
            session.cancel()
            release.set()
            until(lambda: session.status()["state"] == "cancelled")
            stopped = session.status()
            assert stopped["generation"] == discarded and stopped["committed_generation"] == inflight, stopped
            assert not stopped["pending_update"], stopped
            backend.apply = original
            restarted = session.apply(native_scene.capture(rop))
            image(session, restarted)
            report["cases"].append(
                dict(
                    mode=mode,
                    name="Stop discards pending edits; restart captures the current scene",
                    stopped=stopped,
                    after=session.status(),
                )
            )
        finally:
            release.set()
            backend.apply = original
    finally:
        session.close()
        assert session.status()["state"] == "closed" and session.status()["owned_geometry_bytes"] == 0
        save()
    print(json.dumps(dict(mode=mode, result="passed")))
hero.parm("height").set(height)
camera.parm("tx").set(camera_x)
report["result"] = "passed"
save()
print(json.dumps(dict(result="passed", report=str(report_path))))
