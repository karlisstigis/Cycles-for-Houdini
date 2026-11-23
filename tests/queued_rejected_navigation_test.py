# SPDX-License-Identifier: Apache-2.0
"""A camera repair must apply rejected complete geometry before clearing pending."""

from support.package_loader import load_module
from support.paths import PACKAGE
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from concurrent.futures import Future

sys.path.insert(0, str(PACKAGE.parent))
from cycles_houdini import Session

package = ModuleType("rejected_navigation_test_host")
package.Session = Session
package.default_device = lambda: "CPU"
package._native = SimpleNamespace(
    status=lambda capture: dict(state="idle", error="", message=""),
    snapshot_color_identity=lambda desired: "policy",
    navigation_info=lambda nav: dict(root_revision=0),
    navigation_snapshot=lambda target, nav, *args: dict(target, camera=nav),
    navigation_context_equal=lambda desired, nav: True,
    navigation_basis_equal=lambda target, accepted: target["geometry"] == accepted["geometry"],
)
sys.modules[package.__name__] = package
source = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/queued.py"
package.__path__ = [str(source.parent)]
module = load_module(package, "queued")


class Backend:
    def __init__(self, *args, on_start=None):
        self.generation = 0
        self.state = "rendering"
        self.applied = []
        self.rejected = 0

    def poll(self):
        return dict(
            success=True,
            status=dict(state=self.state, generation=self.generation, error="", message=""),
            diagnostics={},
            renderer_pid=123,
            frame=None,
        )

    def apply(self, desired, acknowledged):
        if desired["camera"] == "missing-iris":
            self.rejected += 1
            return dict(self.poll(), success=False, update_rejected=True, error="Cannot open image iris")
        self.generation += 1
        self.state = "rendering"
        self.applied.append(dict(desired))
        return self.poll()

    def control(self, command, value=None):
        if command == "cancel":
            self.state = "cancelled"
        return self.poll()

    def close(self):
        return dict(state="closed", error="", owned_geometry_bytes=0, device_allocation_bytes=0)


module.ExternalBackend = Backend


def until(condition):
    deadline = time.monotonic() + 3
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("Queued repair did not progress")
        time.sleep(0.001)


def submit(controller, geometry, camera="valid"):
    sequence = controller.reserve()
    controller.require_scene(sequence)
    controller.submit(dict(geometry=geometry, camera=camera), sequence)
    return sequence


def navigate(controller, camera="repaired"):
    sequence = controller.reserve()
    controller.submit_navigation(camera, sequence, final=True)
    return sequence


def baseline():
    controller = module.QueuedController(None)
    sequence = submit(controller, "original")
    until(lambda: controller.completed_scene == sequence)
    return controller


# The preview alone is allowed to improve the old accepted image. It must not
# discharge the full geometry update that failed due to a missing camera asset.
controller = baseline()
entered = threading.Event()
release = threading.Event()
try:
    rejected = submit(controller, "new-geometry", "missing-iris")
    until(lambda: controller.status()["update_rejected"])
    backend = controller.backend
    assert controller.acknowledged["geometry"] == "original"
    rejected_calls = backend.rejected
    time.sleep(0.03)
    assert backend.rejected == rejected_calls, "Invalid snapshots must not retry without new input"
    assert controller.rejected_scene[0] == rejected
    original_prepare = controller._prepare_owned

    def prepare(target, nav, scene):
        if scene and target["geometry"] == "new-geometry":
            entered.set()
            assert release.wait(3)
        return original_prepare(target, nav, scene)

    controller._prepare_owned = prepare
    repaired = navigate(controller)
    assert entered.wait(3)
    until(lambda: controller.committed == repaired)
    assert controller.acknowledged["geometry"] == "original"
    assert controller.completed_scene < rejected and controller.status()["pending_update"]
    release.set()
    until(lambda: controller.completed_scene == rejected and not controller.status()["pending_update"])
    assert controller.acknowledged == dict(geometry="new-geometry", camera="repaired")
    assert controller.backend is backend and not controller.status()["update_rejected"]
    assert controller.rejected_scene is None and controller.scene_repair_retries == 1
finally:
    release.set()
    controller.close()


# A repair can arrive while the renderer is still validating the bad full
# snapshot. Its queued camera preview must also retry that complete target.
entered.clear()
release.clear()


class InflightBackend(Backend):
    def apply(self, desired, acknowledged):
        if desired["camera"] == "missing-iris":
            entered.set()
            assert release.wait(3)
        return super().apply(desired, acknowledged)


module.ExternalBackend = InflightBackend
controller = baseline()
try:
    rejected = submit(controller, "inflight-geometry", "missing-iris")
    assert entered.wait(3)
    repaired = navigate(controller, "inflight-repair")
    release.set()
    until(lambda: controller.completed_scene == rejected and not controller.status()["pending_update"])
    assert controller.committed == repaired
    assert controller.acknowledged == dict(geometry="inflight-geometry", camera="inflight-repair")
    assert controller.scene_repair_retries == 1
finally:
    release.set()
    controller.close()
    module.ExternalBackend = Backend


# New full captures can coalesce while the rejected target's retry is owned.
# Only acceptance of the newest complete geometry can satisfy its requirement.
controller = baseline()
entered.clear()
release.clear()
try:
    rejected = submit(controller, "intermediate", "missing-iris")
    until(lambda: controller.status()["update_rejected"])
    original_prepare = controller._prepare_owned

    def prepare(target, nav, scene):
        if scene and target["geometry"] == "intermediate":
            entered.set()
            assert release.wait(3)
        return original_prepare(target, nav, scene)

    controller._prepare_owned = prepare
    navigate(controller)
    assert entered.wait(3)
    submit(controller, "coalesced", "missing-iris")
    latest = submit(controller, "latest", "missing-iris")
    navigate(controller, "latest-camera")
    assert controller.completed_scene < latest
    release.set()
    until(lambda: controller.completed_scene == latest and not controller.status()["pending_update"])
    assert controller.acknowledged == dict(geometry="latest", camera="latest-camera")
    assert all(row["geometry"] != "coalesced" for row in controller.backend.applied)
    assert controller.rejected_scene is None
finally:
    release.set()
    controller.close()


# An uncaptured newer requirement cannot be discharged by an older retained
# scene, even if a camera-only preview is accepted successfully.
controller = baseline()
try:
    rejected = submit(controller, "rejected-old", "missing-iris")
    until(lambda: controller.status()["update_rejected"])
    newer = controller.reserve()
    controller.require_scene(newer)
    repaired = navigate(controller)
    until(lambda: controller.committed == repaired)
    assert controller.completed_scene < newer and controller.status()["pending_update"]
    assert controller.scene_repair_retries == 0
    assert controller.acknowledged["geometry"] == "original"
    controller.submit(dict(geometry="newly-captured", camera="repaired"), newer)
    until(lambda: controller.completed_scene == newer and not controller.status()["pending_update"])
    assert controller.acknowledged["geometry"] == "newly-captured"
finally:
    controller.close()


# Stop/close release the rejected owned scene and never resurrect its geometry.
controller = baseline()
try:
    submit(controller, "retired", "missing-iris")
    until(lambda: controller.status()["update_rejected"])
    assert controller.rejected_scene is not None
    assert controller.control("cancel").wait(3)
    assert controller.rejected_scene is None
    restarted = navigate(controller)
    until(lambda: controller.committed == restarted)
    assert controller.acknowledged["geometry"] == "original" and not controller.status()["pending_update"]
    with controller.condition:
        controller.navigation = controller.pending_navigation = None
    submit(controller, "closed", "missing-iris")
    until(lambda: controller.status()["update_rejected"])
finally:
    controller.close()
assert controller.rejected_scene is None


# Exercise the actual WorkerSession entry point. A full-source repair publishes
# a cheap camera preview before asynchronous capture finishes. The old rejected
# geometry must not be retried under that newer submission in the meantime.
class DeletedGeometryBackend(Backend):
    def apply(self, desired, acknowledged):
        if desired["geometry"] == "deleted-geometry":
            self.rejected += 1
            return dict(self.poll(), success=False, update_rejected=True, error="Missing geometry")
        return super().apply(desired, acknowledged)


module.ExternalBackend = DeletedGeometryBackend
controller = baseline()
try:
    submit(controller, "deleted-geometry")
    until(lambda: controller.status()["update_rejected"])
    backend = controller.backend
    rejections = backend.rejected
    package._native.validate_update_metadata = lambda payload: None
    package._native.prepare_navigation = lambda *args: "fresh-camera"
    host = ModuleType("hou")
    host.isUIAvailable = lambda: False
    host.time = lambda: 0.0
    previous_host = sys.modules.get("hou")
    sys.modules["hou"] = host
    session = object.__new__(module.WorkerSession)
    session._controller = controller
    session._handle = None
    session._interactive = True
    session._capture_failure = None
    session._in_host_capture = False
    session._capture_closed = False
    session._active_capture = None
    session._pending_capture = None
    requests = []
    session._start_capture = lambda request: requests.append(request)
    repaired = session.apply(dict(geometry=[dict(object_key="source")]))
    assert len(requests) == 1 and requests[0][3] == repaired
    assert controller.scene_repair_retries == 0, "Source repair retried the superseded invalid scene"
    until(lambda: controller.committed == repaired)
    assert controller.required_scene == repaired and controller.completed_scene < repaired
    assert controller.status()["pending_update"] and not controller.status()["update_rejected"]
    assert controller.acknowledged["geometry"] == "original" and backend.rejected == rejections
    controller.submit(dict(geometry="fresh-source", camera="fresh-camera"), repaired)
    until(lambda: controller.completed_scene == repaired and not controller.status()["pending_update"])
    assert controller.acknowledged["geometry"] == "fresh-source" and controller.backend is backend
finally:
    controller.close()
    module.ExternalBackend = Backend
    if previous_host is None:
        sys.modules.pop("hou", None)
    else:
        sys.modules["hou"] = previous_host

# A full new-color capture may publish a speculative camera preview from its
# accepted old-color scene first. Discard that preview without rejecting the
# shared source submission while the complete capture is still outstanding.
controller = baseline()
checked = threading.Event()
original_color_identity = module._native.snapshot_color_identity


def color_identity(desired):
    checked.set()
    return desired.get("color", "policy")


try:
    module._native.snapshot_color_identity = color_identity
    repaired = controller.reserve()
    controller.require_scene(repaired)
    with controller.condition:
        controller.color_identity = "new-policy"
    controller.submit_navigation("new-color-camera", repaired, final=False)
    assert checked.wait(3)
    with controller.condition:
        assert controller.rejection is None
    assert controller.status()["pending_update"] and not controller.status()["update_rejected"]
    controller.submit(dict(geometry="new-color-geometry", camera="new-color-camera", color="new-policy"), repaired)
    until(lambda: controller.completed_scene == repaired and not controller.status()["pending_update"])
    assert controller.acknowledged["geometry"] == "new-color-geometry"
    assert not controller.status()["update_rejected"]
finally:
    controller.close()
    module._native.snapshot_color_identity = original_color_identity

# Background object preparation can also rebase the accepted old-color scene
# before the full new-color capture arrives, with camera previews disabled.
controller = baseline()
checked.clear()
original_navigation_info = module._native.navigation_info
try:
    module._native.snapshot_color_identity = color_identity
    module._native.navigation_info = lambda navigation: dict(root_revision=1)
    repaired = controller.reserve()
    controller.require_scene(repaired)
    with controller.condition:
        controller.color_identity = "new-policy"
    controller.submit_navigation("new-color-objects", repaired, final=False, preview=False)
    assert checked.wait(3)
    with controller.condition:
        assert controller.rejection is None
    assert controller.status()["pending_update"] and not controller.status()["update_rejected"]
    controller.submit(dict(geometry="new-color-geometry", camera="new-color-objects", color="new-policy"), repaired)
    until(lambda: controller.completed_scene == repaired and not controller.status()["pending_update"])
    assert controller.acknowledged["geometry"] == "new-color-geometry"
    assert not controller.status()["update_rejected"]
finally:
    controller.close()
    module._native.snapshot_color_identity = original_color_identity
    module._native.navigation_info = original_navigation_info

# A final navigation-only request has no full capture following it. A stale
# policy must produce the recapture diagnostic instead of leaving it pending.
controller = baseline()
try:
    with controller.condition:
        controller.color_identity = "new-policy"
    navigate(controller, "stale-final-camera")
    until(lambda: controller.status()["update_rejected"])
    assert "recapture scene" in controller.status()["error"]
finally:
    controller.close()


# A color-changing source capture may fail after its obsolete-color preview was
# discarded. Continuous OBJ previews must keep that terminal source error until
# a real source repair arrives; merely moving the camera cannot repair capture.
class ImmediateConversion:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as error:
            future.set_exception(error)
        return future


def capture_session(controller, submission, *, failed=False):
    session = object.__new__(module.WorkerSession)
    session._controller = controller
    session._handle = None
    session._capture_failure = ""
    session._in_host_capture = session._capture_closed = False
    session._pending_capture = None
    session._capture_events = []
    session._capture_coalesced = 0
    session.last_capture_seconds = 0.0
    session.material_paths_revision = 0
    session._conversion = ImmediateConversion()
    session.notify_color_configuration = lambda: None
    future = Future()
    if failed:
        future.set_exception(RuntimeError("intentional capture conversion failure"))
    else:
        future.set_result(0.0)
    session._active_capture = dict(
        job=object(),
        future=future,
        submission=submission,
        stage="convert",
        request=({"geometry": []}, 0.0, False, submission),
        acquisition=0.0,
        scan=0.0,
        preparation=0.0,
        preparation_max=0.0,
        scan_batches=0,
        preparation_yields=0,
    )
    return session


def check_wait_error(session, expected):
    # A waiter for the newest pose must return the source error or Stop even
    # when the converter still owns a future and the last good image is held.
    try:
        session.wait(timeout=1)
    except RuntimeError as error:
        assert expected in str(error), error
    else:
        raise AssertionError("wait() returned a stale image")


previous_host = sys.modules.get("hou")
sys.modules["hou"] = SimpleNamespace(time=lambda: 0.0, isUIAvailable=lambda: False)
original_native = dict(vars(module._native))
try:
    module._native.snapshot_color_identity = color_identity
    module._native.native_stats = lambda handle: {}
    module._native.frame = lambda image: dict(image, complete=True)
    module._native.status = lambda handle: original_native["status"](handle) | dict.fromkeys(
        (
            "cooking_seconds",
            "extraction_seconds",
            "extractions",
            "cache_hits",
            "packed_instances",
            "index_instance_ids",
        ),
        0,
    )
    module._native.frame_info = lambda image: image
    module._native.prepare_object_navigation = lambda *args: "continuous-camera"
    module._native.acquire_images = lambda job: False
    discarded = []
    module._native.discard_apply = lambda job: discarded.append(job) or 0.0

    for failure in ("conversion", "finalization", "pending-acquisition"):
        controller = baseline()
        try:
            retained = controller.backend
            frame = dict(generation=controller.physical, sequence=1)
            controller.accept(dict(retained.poll(), frame=frame))
            last_good = controller.frame()
            failed = controller.reserve()
            controller.require_scene(failed)
            with controller.condition:
                controller.color_identity = "new-policy"
            checked.clear()
            controller.submit_navigation("speculative-camera", failed, final=False)
            assert checked.wait(3), "Speculative old-policy preview was not examined"
            assert not controller.status()["update_rejected"]
            session = capture_session(controller, failed, failed=failure == "conversion")
            if failure == "finalization":

                def fail_finish(job):
                    raise RuntimeError("intentional capture finalization failure")

                module._native.finish_apply = fail_finish
            elif failure == "pending-acquisition":
                # This older capture was already cancelled; starting the newest
                # coalesced request fails synchronously on the next host pump.
                controller.cancelled_through = failed
                newer = controller.reserve()
                controller.require_scene(newer)
                session._pending_capture = ({"geometry": []}, 0.0, False, newer)

                def fail_start(request):
                    raise RuntimeError("intentional pending acquisition failure")

                session._start_capture = fail_start
            before_discard = len(discarded)
            session._pump_capture()
            session._pump_capture()
            assert session._active_capture is None and len(discarded) == before_discard + 1
            assert controller.status()["update_rejected"], failure
            message = controller.status()["error"]
            assert "intentional" in message
            for _ in range(20):
                assert session.preview_object_poses({}, at_time=0.0) is not None
                status = controller.status()
                assert status["update_rejected"] and status["error"] == message, (failure, status)
                assert not status["pending_update"], (failure, status)
            assert controller.frame() == last_good and controller.backend is retained
            check_wait_error(session, message)
            assert controller.acknowledged["geometry"] == "original"
            repaired = controller.reserve()
            controller.require_scene(repaired)
            controller.reject_capture(failed, "late failure from previous capture")
            assert not controller.status()["update_rejected"] and controller.status()["pending_update"]
            # The new complete capture must carry the current policy.
            module._native.finish_apply = lambda job: dict(
                geometry="recovered-source", camera="repaired", color="new-policy"
            )
            recovered = capture_session(controller, repaired)
            recovered._pump_capture()
            assert recovered._active_capture is None
            until(lambda: controller.completed_scene == repaired and not controller.status()["pending_update"])
            assert not controller.status()["update_rejected"] and controller.backend is retained
            assert controller.acknowledged["geometry"] == "recovered-source"
        finally:
            controller.close()

    # Stop retires an active capture even when conversion itself reports an
    # error. It keeps the accepted pixels, does not invent an edit error, and a
    # subsequent source capture can use the same backend.
    for conversion_fails in (False, True):
        controller = baseline()
        try:
            retained = controller.backend
            frame = dict(generation=controller.physical, sequence=1)
            controller.accept(dict(retained.poll(), frame=frame))
            last_good = controller.frame()
            cancelled = controller.reserve()
            controller.require_scene(cancelled)
            with controller.condition:
                controller.color_identity = "new-policy"
            controller.submit_navigation("cancelled-preview", cancelled, final=False)
            session = capture_session(controller, cancelled, failed=conversion_fails)
            conversion = session._active_capture["future"] = Future()
            before_discard = len(discarded)
            session.cancel()
            until(lambda: controller.status()["state"] == "cancelled")
            session._pump_capture()
            assert session._active_capture is not None, "Stop waited for the converter"
            check_wait_error(session, "cancelled")
            assert not controller.status()["pending_update"] and controller.frame() == last_good
            if conversion_fails:
                conversion.set_exception(RuntimeError("intentional cancelled conversion failure"))
            else:
                conversion.set_result(0.0)
            session._pump_capture()
            session._pump_capture()
            assert session._active_capture is None and len(discarded) == before_discard + 1
            assert not controller.status()["pending_update"] and not controller.status()["update_rejected"]
            assert controller.frame() == last_good and controller.backend is retained
            repaired = controller.reserve()
            controller.require_scene(repaired)
            controller.submit(dict(geometry="after-stop", camera="repaired", color="new-policy"), repaired)
            until(lambda: controller.completed_scene == repaired and not controller.status()["pending_update"])
            assert controller.backend is retained and controller.status()["state"] == "rendering"
        finally:
            controller.close()
finally:
    vars(module._native).clear()
    vars(module._native).update(original_native)
    if previous_host is None:
        sys.modules.pop("hou", None)
    else:
        sys.modules["hou"] = previous_host

print(
    "PASS: scene repair, continuous color capture failures, retained frames, bounded Stop cleanup and same-backend recovery"
)
