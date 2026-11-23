# SPDX-License-Identifier: Apache-2.0
"""Retained geometry navigation matches fresh capture, including packed shutter poses."""

from support.paths import artifacts_root
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import _native

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage1/navigation"
dest.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "navigation_mixed", run_init_scripts=False)
box = obj.createNode("box")
box.parmTuple("size").set((0.3, 0.4, 0.3))
line = obj.createNode("line")
line.parm("dist").set(0.45)
line_x = obj.createNode("xform")
line_x.setInput(0, line)
line_x.setParms(dict(tx=0.4, ty=-0.22))
width = obj.createNode("attribwrangle")
width.setInput(0, line_x)
width.parm("snippet").set("f@width=.06;")
point = obj.createNode("add")
point.parm("points").set(1)
point.setParms(dict(usept0=1, pt0x=-0.4))
radius = obj.createNode("attribwrangle")
radius.setInput(0, point)
radius.parm("snippet").set("f@pscale=.09;")
merge = obj.createNode("merge")
merge.setInput(0, box)
merge.setInput(1, width)
merge.setInput(2, radius)
deform = obj.createNode("xform", "prototype_change")
deform.setInput(0, merge)
targets = obj.createNode("grid")
targets.setParms(dict(rows=2, cols=2, orient=0))
targets.parmTuple("size").set((1.6, 1.1))
move = obj.createNode("xform")
move.setInput(0, targets)
copies = obj.createNode("copytopoints")
copies.setInput(0, deform)
copies.setInput(1, move)
copies.parm("pack").set(1)
copies.setDisplayFlag(True)
copies.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "navigation_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=5))
now = 0.25
checks = []


def handle(capture=True):
    return _native.create(device, 320, 192, 128, False, True, True, False, None, capture)


def scene(motion):
    return dict(
        geometry=[ch.geometry_request(obj, copies, material="plain", material_mode="uniform", at_time=now)],
        materials=[dict(key="plain", kind="emission", color=(0.2, 0.7, 0.1), strength=1)],
        camera_path=camera.path(),
        background=(0, 0, 0),
        motion=motion,
    )


def render(snapshot, capture):
    engine = _native.engine_create(capture)
    try:
        generation = _native.apply_snapshot(engine, snapshot)
        start = time.monotonic()
        while time.monotonic() - start < 90:
            status = _native.status(engine)
            assert status["state"] != "failed", status
            image = _native.frame(engine)
            if image and image["generation"] == generation and image["complete"]:
                return np.frombuffer(image["planes"]["combined"]["pixels"], np.float32).copy()
            time.sleep(0.01)
        raise TimeoutError(status)
    finally:
        _native.close(engine)


def compare(candidate, reference, capture, label):
    actual = render(candidate, capture)
    expected = render(reference, capture)
    difference = float(np.abs(actual - expected).mean())
    assert difference < 0.002, (label, difference)
    assert (actual[0::4] > 0.1).sum() > 500, label
    checks.append(dict(check=label, mean_difference=difference))


for enabled, samples, offset in ((False, 3, 0), (True, 3, 0), (True, 5, 0.5)):
    for parm in (obj.parm("tx"), obj.parm("ry"), move.parm("tx"), camera.parm("tx")):
        parm.deleteAllKeyframes()
        parm.set(0)
    obj.parmTuple("s").set((1, 1, 1))
    deform.parm("rz").set(0)
    motion = dict(enabled=enabled, fps=24.0, shutter_frames=0.5, offset=offset, samples=samples)
    if enabled:
        move.parm("tx").setExpression("$T*1.2", hou.exprLanguage.Hscript)
    h = handle()
    reference_handle = handle()
    try:
        baseline = _native.apply(h, scene(motion), now)
        initial_info = _native.snapshot_info(baseline)
        full_nav = _native.prepare_navigation(h, baseline, scene(motion), now)
        assert _native.navigation_info(full_nav)["root_overrides"] == 1
        root_revision = _native.navigation_info(full_nav)["root_revision"]
        camera_nav = _native.prepare_navigation(
            h, baseline, dict(camera_path=camera.path(), motion=motion), now, full_nav
        )
        assert _native.navigation_info(camera_nav)["root_revision"] == root_revision
        assert _native.navigation_snapshot(baseline, camera_nav, False, True) is None
        retained = _native.navigation_snapshot(baseline, camera_nav, True, True)
        assert retained is not None and _native.navigation_basis_equal(baseline, retained)
        engine = _native.engine_create(h)
        try:
            generation = _native.apply_snapshot(engine, baseline)
            assert _native.apply_snapshot(engine, retained, baseline) == generation
            assert _native.status(engine)["unchanged_reuses"] >= 1
        finally:
            _native.close(engine)
        checks.append(
            f"Camera-only no-op shares object ownership and permits identical-scene accumulation reuse; blur={enabled}, samples={samples}"
        )
        empty_nav = _native.prepare_navigation(h, baseline, dict(motion=motion, geometry=[]), now, full_nav)
        assert _native.navigation_info(empty_nav)["root_overrides"] == 0
        assert _native.navigation_info(full_nav)["root_overrides"] == 1
        checks.append(
            f"Authoritative root replacement retires removed overrides without mutating previous navigation; blur={enabled}, samples={samples}"
        )
        for scale in ((1, 1, 1), (-1.1, 0.8, 1.2)):
            obj.parmTuple("s").set(scale)
            obj.parm("tx").setExpression(".18+$T*.7", hou.exprLanguage.Hscript)
            obj.parm("ry").setExpression("8+$T*12", hou.exprLanguage.Hscript)
            camera.parm("tx").set(0.13)
            nav = _native.prepare_navigation(h, baseline, scene(motion), now)
            assert nav is not None
            with ThreadPoolExecutor(max_workers=1) as pool:
                candidate = pool.submit(_native.navigation_snapshot, baseline, nav).result()
            assert candidate is not None
            fresh = _native.apply(reference_handle, scene(motion), now)
            assert _native.snapshot_info(candidate) == initial_info
            compare(
                candidate,
                fresh,
                h,
                f"Packed mixed origins; blur={enabled}, samples={samples}, offset={offset}, scale={scale}",
            )
            camera_preview = _native.navigation_snapshot(baseline, nav, False, True)
            assert camera_preview is not None and _native.navigation_basis_equal(baseline, camera_preview)
            # Specify the old static OBJ matrix independently; packed local
            # motion is still freshly captured from its native SOP sources.
            camera_reference = scene(motion)
            camera_reference["geometry"][0]["object_path"] = ""
            camera_reference["geometry"][0]["transform"] = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
            fresh_camera = _native.apply(reference_handle, camera_reference, now)
            compare(
                camera_preview,
                fresh_camera,
                h,
                f"Camera-only preview keeps old objects while new transforms prepare; blur={enabled}, samples={samples}, scale={scale}",
            )
        future = _native.prepare_navigation(h, baseline, scene(motion), now + 1)
        assert future is not None and _native.navigation_snapshot(baseline, future) is None
        assert _native.prepare_navigation(h, baseline, dict(camera_path=camera.path(), motion=motion), now + 1) is None
        other = scene(dict(motion, samples=7))
        future = _native.prepare_navigation(h, baseline, other, now)
        assert future is not None and _native.navigation_snapshot(baseline, future) is None
        assert (
            _native.prepare_navigation(h, baseline, dict(camera_path=camera.path(), motion=other["motion"]), now)
            is None
        )
        other_snapshot = _native.apply(reference_handle, other, now)
        assert not _native.navigation_context_equal(other_snapshot, nav)
        assert _native.navigation_context_equal(baseline, nav)
        checks.append(f"Time and motion-context changes cannot reuse origins; blur={enabled}, samples={samples}")
        # Frozen navigation must remain valid with the normal capture handle
        # exclusively owned by another job, without waiting for its conversion.
        deform.parm("rz").set(21)
        job = _native.prepare_apply(h, scene(motion), now)
        entered = threading.Event()
        release = threading.Event()

        def held():
            entered.set()
            assert release.wait(10)
            return _native.convert_apply(job)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(held)
            assert entered.wait(2)
            obj.parm("tx").deleteAllKeyframes()
            obj.parm("tx").set(0.52)
            camera.parm("tx").set(-0.17)
            nav = _native.prepare_navigation(h, baseline, scene(motion), now)
            preview = _native.navigation_snapshot(baseline, nav)
            assert preview is not None and not future.done()
            assert _native.snapshot_info(baseline) == initial_info
            release.set()
            future.result()
        converted = _native.finish_apply(job)
        combined = _native.navigation_snapshot(converted, nav)
        assert combined is not None
        fresh = _native.apply(reference_handle, scene(motion), now)
        compare(
            combined, fresh, h, f"Latest root/camera merged with changed prototype; blur={enabled}, samples={samples}"
        )
        # Capturing a singular source does not destroy its directly recorded
        # local poses. Restoring scale needs no inverse of that source matrix.
        obj.parmTuple("s").set((0, 1, 1))
        singular = _native.apply(h, scene(motion), now)
        obj.parmTuple("s").set((1, 1, 1))
        nav = _native.prepare_navigation(h, singular, scene(motion), now)
        restored = _native.navigation_snapshot(singular, nav)
        fresh = _native.apply(reference_handle, scene(motion), now)
        compare(
            restored,
            fresh,
            h,
            f"Restore from singular captured root without inversion; blur={enabled}, samples={samples}",
        )
    finally:
        _native.close(h)
        _native.close(reference_handle)

# A genuine failed geometry transaction after a successful camera preview must
# remain visible as a rejection, rather than waiting forever on an older source
# submission or destroying the accepted renderer. Repair the same live worker.
motion = dict(enabled=False, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3)
original_convert = _native.convert_apply
entered = threading.Event()
release = threading.Event()


def failed_conversion(job):
    entered.set()
    assert release.wait(8)
    raise RuntimeError("Intentional held-conversion failure for navigation recovery")


with ch.Session(
    device=device, width=320, height=192, samples=32, interactive=True, execution="worker", collect_diagnostics=True
) as session:
    session.wait(session.apply(scene(motion), at_time=now))
    pid = session.status()["renderer_pid"]
    completed = session.frame()
    revision = session.status()["render_revision"]
    same = session.wait(session.apply(dict(camera_path=camera.path(), motion=motion), at_time=now), timeout=10)
    assert session.status()["render_revision"] == revision
    assert same["planes"]["combined"]["pixels"] == completed["planes"]["combined"]["pixels"]
    session.wait(session.apply(scene(motion), at_time=now, reuse_if_unchanged=True), timeout=10)
    assert session.status()["render_revision"] == revision
    checks.append(
        "No-op camera and full refresh advance logical ownership while retaining the exact completed physical image"
    )
    try:
        _native.convert_apply = failed_conversion
        deform.parm("rz").set(43)
        geometry = session.apply(scene(motion), at_time=now)
        began = time.monotonic()
        while not entered.is_set():
            session.status()
            assert time.monotonic() - began < 5
            time.sleep(0.005)
        camera.parm("tx").set(0.28)
        latest = session.apply(dict(camera_path=camera.path(), motion=motion), at_time=now)
        began = time.monotonic()
        while True:
            status = session.status()
            image = session.frame_info()
            if image and image["generation"] == latest and image.get("current_scene"):
                break
            assert status["state"] != "failed" and time.monotonic() - began < 5, status
            time.sleep(0.005)
        assert status["pending_update"] and status["capture_pending"]
        assert status["renderer_pid"] == pid and latest > geometry
        release.set()
        began = time.monotonic()
        while session.status()["state"] != "failed":
            assert time.monotonic() - began < 5
            time.sleep(0.005)
        rejected = session.status()
        assert rejected["update_rejected"] and not rejected["pending_update"]
        assert "Intentional held-conversion failure" in rejected["error"], rejected
        assert rejected["backend_alive"] and rejected["renderer_pid"] == pid
        assert session.frame_info()["generation"] == latest
    finally:
        release.set()
        _native.convert_apply = original_convert
    session.wait(session.apply(scene(motion), at_time=now))
    assert not session.status()["update_rejected"] and session.status()["renderer_pid"] == pid
    checks.append(
        "Failed held geometry after navigation preview reports rejection, retains the worker/image, and repairs live"
    )
    session._controller.backend.process.kill()
    began = time.monotonic()
    while session.status()["state"] != "failed":
        assert time.monotonic() - began < 5
        time.sleep(0.005)
    obj.parm("tx").set(0.1)
    camera.parm("tx").set(-0.35)
    repaired = session.wait(session.apply(scene(motion), at_time=now))
    recovered_pixels = np.frombuffer(repaired["planes"]["combined"]["pixels"], np.float32).copy()
    assert session.status()["renderer_pid"] != pid
reference_handle = handle()
try:
    fresh = _native.apply(reference_handle, scene(motion), now)
    difference = float(np.abs(render(fresh, reference_handle) - recovered_pixels).mean())
    assert difference < 0.002, difference
    checks.append(
        dict(
            check="A new worker does not inherit stale preview camera/root poses after process failure",
            mean_difference=difference,
        )
    )
finally:
    _native.close(reference_handle)

# Force a root preview to finish before the identical full capture is released.
# The completed capture must ACK without resetting the already converged image.
entered = threading.Event()
release = threading.Event()


def delayed_transform_capture(job):
    entered.set()
    assert release.wait(8)
    return original_convert(job)


with ch.Session(
    device=device, width=320, height=192, samples=32, interactive=True, execution="worker", collect_diagnostics=True
) as session:
    session.wait(session.apply(scene(motion), at_time=now))
    try:
        _native.convert_apply = delayed_transform_capture
        obj.parm("tx").set(0.36)
        current = session.apply(scene(motion), at_time=now)
        began = time.monotonic()
        while True:
            status = session.status()
            preview = session.frame()
            if (
                entered.is_set()
                and preview
                and preview["generation"] == current
                and preview.get("current_scene")
                and preview["complete"]
            ):
                break
            assert time.monotonic() - began < 6 and status["state"] != "failed", status
            time.sleep(0.005)
        assert status["pending_update"] and status["committed_scene_submission"] < current
        revision = status["render_revision"]
        pixels = preview["planes"]["combined"]["pixels"]
        release.set()
        _native.convert_apply = original_convert
        finished = session.wait(current, timeout=10)
        assert session.status()["render_revision"] == revision
        assert finished["planes"]["combined"]["pixels"] == pixels
        assert session.status()["unchanged_reuses"] >= 1
        checks.append(
            "An identical completed capture acknowledges its converged object preview without resetting accumulation"
        )
    finally:
        release.set()
        _native.convert_apply = original_convert

report = dict(
    result="passed",
    device=device,
    checks=checks,
    scope="Independent fresh-capture references validate owned rebasing, not total converter correctness. Held jobs verify no adapter reentry. Rendering uses one engine at a time; GUI presentation has a separate test.",
)
(dest / "houdini-navigation-snapshot-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks), device=device)))
