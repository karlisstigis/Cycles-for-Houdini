# SPDX-License-Identifier: Apache-2.0
"""Exercise artist preview pacing, controls and hidden/stalled viewer recovery."""

from support.paths import artifacts_root

from support.package_loader import load_module
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

package = ModuleType("feedback_test_host")
package.Session = object
package.default_device = lambda: "CPU"
package._native = SimpleNamespace(status=lambda capture: dict(state="idle", error="", message=""))
package._native.snapshot_color_identity = (
    lambda desired: "new-policy" if desired == "new-policy-scene" else "old-policy"
)
sys.modules[package.__name__] = package
source = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/queued.py"
package.__path__ = [str(source.parent)]
module = load_module(package, "queued")


class Backend:
    def __init__(self, capture, desired, *, on_start=None):
        self.state = "rendering"
        self.generation = 0
        self.scenes = []
        self.polls = 0

    def poll(self):
        self.polls += 1
        return dict(
            success=True,
            status=dict(state=self.state, generation=self.generation, error="", message=""),
            diagnostics={},
            renderer_pid=123,
            frame=None,
        )

    def apply(self, desired, acknowledged):
        self.generation += 1
        if self.state != "paused":
            self.state = "rendering"
        self.scenes.append((desired, acknowledged))
        return self.poll()

    def control(self, command, value=None):
        if command == "cancel":
            self.state = "cancelled"
        elif command == "pause":
            self.state = "paused" if value else "rendering"
        return self.poll()

    def close(self):
        return dict(state="closed", error="", owned_geometry_bytes=0, device_allocation_bytes=0)


module.ExternalBackend = Backend
controller = module.QueuedController(None)


def until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("Preview pacing did not progress")
        time.sleep(0.001)


try:
    controller.set_feedback("visible-viewer", True)
    first = controller.submit("first")
    until(lambda: controller.status()["committed_generation"] == first)
    # A renderer that has not produced any pixels must not leave the worker
    # probing rapidly forever. Normal polling continues after the short burst.
    until(lambda: controller.first_frame_fast_polls > 0)
    time.sleep(0.12)
    fast_polls = controller.first_frame_fast_polls
    polls = controller.backend.polls
    time.sleep(0.05)
    assert controller.first_frame_fast_polls == fast_polls
    assert controller.backend.polls > polls
    controller.submit("obsolete")
    latest = controller.submit("latest")
    until(lambda: controller.status()["waiting_for_presentation"])
    preference = controller.control("preview", "position")
    assert controller.committed == first
    controller.presented(first + 100)  # An unrelated/stale paint cannot ACK it.
    assert controller.committed == first
    controller.presented(first)
    until(lambda: controller.committed == latest)
    assert preference.wait(3)
    assert controller.backend.scenes == [("first", None), ("latest", "first")]

    # A future pass preference must not prevent Pause from reaching the
    # accepted scene while the image has not yet been painted. Scene edits
    # may commit while paused; they must retain that pause intent.
    pause_update = controller.submit("edit-while-paused")
    until(lambda: controller.status()["waiting_for_presentation"])
    preference = controller.control("preview", "combined")
    paused = controller.control("pause", True)
    assert paused.wait(3)
    until(lambda: controller.committed == pause_update)
    assert controller.cached["status"]["state"] == "paused" and preference.wait(3)
    resumed = controller.control("pause", False)
    assert resumed.wait(3)
    assert controller.cached["status"]["state"] == "rendering"

    # Closing/hiding the last visible viewer removes the paint requirement.
    hidden = controller.submit("hidden-viewer-update")
    until(lambda: controller.status()["waiting_for_presentation"])
    controller.set_feedback("visible-viewer", False)
    until(lambda: controller.committed == hidden)
    controller.set_feedback("visible-viewer", True)

    # Stop retires pending work without waiting for a paint. A later Render
    # must still submit and reuse the same persistent backend.
    controller.submit("discard-on-stop")
    until(lambda: controller.status()["waiting_for_presentation"])
    stopped = controller.control("cancel")
    assert stopped.wait(3) and controller.cached["status"]["state"] == "cancelled"
    assert controller.committed == hidden
    restart = controller.submit("restart")
    until(lambda: controller.committed == restart)
    assert all(scene[0] != "discard-on-stop" for scene in controller.backend.scenes)

    # A visible but stalled viewer must not block scene updates indefinitely.
    submitted = time.perf_counter()
    fallback = controller.submit("fallback")
    until(lambda: controller.committed == fallback)
    assert time.perf_counter() - submitted < 1.5
    # A color-policy notification is independent of rendering a new scene.
    # It must bypass a stalled preview and reject a queued snapshot captured
    # before that notification, rather than silently restoring the old policy.
    old_backend = controller.backend
    accepted = controller.committed
    obsolete = controller.submit("obsolete-color-scene")
    until(lambda: controller.status()["waiting_for_presentation"])
    notified = controller.control("color", ("new-policy", b"owned-policy"))
    assert notified.wait(3)
    controller.set_feedback("visible-viewer", False)
    until(lambda: controller.status()["update_rejected"])
    assert controller.committed == accepted and controller.backend is old_backend
    assert all(scene[0] != "obsolete-color-scene" for scene in old_backend.scenes)
    recovered = controller.submit("new-policy-scene")
    until(lambda: controller.committed == recovered)
    assert not controller.status()["update_rejected"] and controller.backend is old_backend
finally:
    controller.close()
assert controller.closed and not controller.thread.is_alive()

# Finishing an already owned scene prevents starvation during continuous edits.
# A safe pre-transaction rejection of that older scene must also leave the newer
# pending repair reachable. Current invalid input must still be reported.
entered = threading.Event()
release = threading.Event()


class RejectingBackend(Backend):
    def apply(self, desired, acknowledged):
        if desired == "rejected-older-scene":
            return dict(
                self.poll(),
                success=False,
                update_rejected=True,
                error="Intentional pre-transaction validation rejection",
            )
        return super().apply(desired, acknowledged)


module.ExternalBackend = RejectingBackend
controller = module.QueuedController(None)


def prepare(target, navigation, scene):
    if target == "rejected-older-scene":
        entered.set()
        assert release.wait(3), "Owned preparation was not released"
    return target, 0.0


controller._prepare_owned = prepare


def submit_scene(scene):
    submission = controller.reserve()
    controller.require_scene(submission)
    controller.submit(scene, submission)
    return submission


try:
    first = submit_scene("baseline")
    until(lambda: controller.committed == first)
    retained = controller.backend
    older = submit_scene("rejected-older-scene")
    assert entered.wait(3)
    submit_scene("coalesced-repair")
    repaired = submit_scene("latest-repair")
    release.set()

    def repaired_without_failure():
        status = controller.status()
        assert status["state"] != "failed", status
        return controller.completed_scene == repaired

    until(repaired_without_failure)
    assert controller.backend is retained and controller.committed == repaired
    assert any(
        e["scene_submission"] == older and not e["accepted"] and e["intermediate_preview"] for e in controller.events
    )
    assert [s[0] for s in retained.scenes] == ["baseline", "latest-repair"]

    current = submit_scene("rejected-older-scene")
    until(lambda: controller.status()["update_rejected"])
    assert controller.rejection[0] == current
    assert controller.backend is retained and controller.acknowledged == "latest-repair"
    repaired = submit_scene("second-repair")
    until(lambda: controller.completed_scene == repaired)
    assert not controller.status()["update_rejected"] and controller.backend is retained
finally:
    release.set()
    controller.close()
assert controller.closed and not controller.thread.is_alive()

# Owned pixels and the accepted physical/logical identity must be read under
# one lock. Duplicate/obsolete backend frames do not wake the viewer, while a
# retained physical scene acknowledged as a newer logical request does.
package._native.frame_info = lambda image: image
package._native.sample_focus = lambda image, x, y: dict(
    generation=image["generation"], sequence=image["sequence"], position=(x, y, 1), depth=1
)
controller = module.QueuedController(None)
notifications = []


def wakeup():
    assert not controller.condition._is_owned()
    notifications.append(controller.display_state())


def frame_reply(physical, sequence):
    return dict(
        success=True,
        status=dict(state="rendering", generation=physical, error="", message=""),
        diagnostics={},
        renderer_pid=123,
        frame=dict(generation=physical, sequence=sequence),
    )


try:
    controller.set_frame_wakeup("viewer", wakeup)
    first = frame_reply(1, 1)
    controller.accept(first, 1)
    assert controller.display_state() == (1, 1, (first["frame"], 1, 1))
    assert controller.display_state(1) == (1, 1, None)
    controller.control("preview", ("combined", True))
    focus = dict(generation=1, sequence=2)
    controller.accept(dict(first, frame=None, focus_frame=focus))
    assert controller.focus_sample(4, 7)["render_revision"] == 1
    assert len(notifications) == 1 and "focus_frame" not in controller.cached
    controller.accept(frame_reply(1, 1))
    assert len(notifications) == 1
    controller.accept(dict(first, frame=None), 2)
    assert controller.display_state() == (2, 1, (first["frame"], 2, 2))
    assert controller.focus_sample(4, 7)["generation"] == 2
    assert len(notifications) == 2
    controller.accept(frame_reply(0, 99))
    assert len(notifications) == 2 and controller.display_state()[0:2] == (2, 1)
    controller.accept(dict(frame_reply(2, 1), frame=None), 3)
    assert controller.display_state() == (3, 2, None)
    assert controller.focus_sample(4, 7) is None
    controller.accept(dict(first, frame=None, focus_frame=focus))
    assert controller.focus_sample(4, 7) is None
    controller.accept(frame_reply(2, 1))
    assert len(notifications) == 3 and controller.display_state()[2][1:] == (3, 3)
    controller.set_frame_wakeup("viewer", None)

    reader_started = threading.Event()

    def publish():
        for physical in range(3, 203):
            controller.accept(frame_reply(physical, 1), physical + 1)
            if physical == 3:
                assert reader_started.wait(3), "Concurrent identity reader did not run"
            time.sleep(0)

    producer = threading.Thread(target=publish)
    producer.start()
    reads = 0
    while producer.is_alive():
        logical, physical, owned = controller.display_state()
        assert owned and owned[1] == logical and owned[0]["generation"] == physical
        reads += 1
        reader_started.set()
        time.sleep(0)
    producer.join()
    assert reads and len(notifications) == 3
finally:
    controller.close()
assert controller.frame_wakeups == {} and not controller.thread.is_alive()
assert controller.focus_image is None


# Host transform evaluation can call back into status/capture. An early pose
# acquisition must leave source loading untouched even under that re-entry.
class PoseController:
    def __init__(self):
        self.condition = threading.RLock()
        self.closing = False
        self.cached = dict(status=dict(state="rendering"))
        self.acknowledged = object()
        self.navigation = None
        self.requested = 1
        self.cancelled_through = 0
        self.epoch = 1
        self.required_scene = 0
        self.submissions = []

    def reserve(self):
        self.requested += 1
        return self.requested

    def require_scene(self, submission, *, capture=True):
        self.required_scene = submission

    def submit_navigation(self, navigation, submission):
        self.submissions.append((navigation, submission))


def pose_session():
    session = module.WorkerSession.__new__(module.WorkerSession)
    session._capture_closed = False
    session._in_host_capture = False
    session._handle = object()
    session._capture_failure = ""
    session._controller = PoseController()
    session.nested_source = []
    session._advance_capture = lambda: session.nested_source.append("advanced")
    return session


old_hou = sys.modules.get("hou")
sys.modules["hou"] = SimpleNamespace(time=lambda: 0.0, isUIAvailable=lambda: False)
try:
    session = pose_session()

    def nested_acquisition(*args):
        session._pump_capture()
        assert session.preview_object_poses({}, at_time=0.0) is None
        try:
            session.apply({}, at_time=0.0)
        except RuntimeError as error:
            assert "reentered" in str(error)
        else:
            raise AssertionError("Source apply reentered pose acquisition")
        return "owned-pose"

    package._native.prepare_object_navigation = nested_acquisition
    assert session.preview_object_poses({}, at_time=0.0) == 2
    assert session.nested_source == [] and not session._in_host_capture
    assert session._controller.required_scene == 2 and session._controller.submissions == [("owned-pose", 2)]

    session = pose_session()

    def failed_acquisition(*args):
        raise RuntimeError("Transform evaluation failed")

    package._native.prepare_object_navigation = failed_acquisition
    try:
        session.preview_object_poses({}, at_time=0.0)
    except RuntimeError as error:
        assert "Transform evaluation failed" in str(error)
    else:
        raise AssertionError("Acquisition error was swallowed")
    assert not session._in_host_capture and session._controller.requested == 1

    for change in ("stop", "pause", "epoch", "closing", "closed"):
        session = pose_session()

        def superseded_acquisition(*args):
            controller = session._controller
            if change == "stop":
                controller.cancelled_through = controller.requested
            elif change == "pause":
                controller.cached["status"]["state"] = "paused"
            elif change == "epoch":
                controller.epoch += 1
            elif change == "closing":
                controller.closing = True
            else:
                session._capture_closed = True
            return "obsolete-pose"

        package._native.prepare_object_navigation = superseded_acquisition
        assert session.preview_object_poses({}, at_time=0.0) is None, change
        assert session._controller.requested == 1 and not session._controller.submissions
        assert not session._in_host_capture

    def forbidden_acquisition(*args):
        raise AssertionError("Inactive pose acquisition ran")

    package._native.prepare_object_navigation = forbidden_acquisition
    for state in ("idle", "paused", "cancelled", "closed", "failed", "stopping-before-ack"):
        session = pose_session()
        if state == "stopping-before-ack":
            session._controller.cancelled_through = 1
        else:
            session._controller.cached["status"]["state"] = state
        assert session.preview_object_poses({}, at_time=0.0) is None, state

    # Noninteractive workers still expose explicit pose previews. Refresh the
    # supplied source fields without publishing a camera-only render, and keep
    # untouched camera/light fields through partial source updates.
    class SourceController(PoseController):
        def submit_navigation(self, navigation, submission, *, final=False, preview=True):
            self.navigation = (submission, navigation, 0)
            self.submissions.append((navigation, submission, final, preview))

    session = pose_session()
    session._interactive = False
    session._controller = SourceController()
    session._controller.navigation = (1, dict(camera="retained-camera", lights="old-override"), 0)
    session._active_capture = session._pending_capture = None
    captures = []
    session._start_capture = captures.append
    package._native.validate_update_metadata = lambda value: None
    prepared_sources = []

    def source_navigation(capture, baseline, payload, at_time, previous):
        prepared_sources.append(dict(payload))
        return dict(previous, **payload)

    package._native.prepare_navigation = source_navigation
    source = session.apply(dict(lights="new-authoritative-light"), at_time=0)
    assert session._controller.navigation[1] == dict(camera="retained-camera", lights="new-authoritative-light")
    assert session._controller.submissions[-1][1:] == (source, False, False)
    retained_navigation = session._controller.navigation
    session.apply(dict(materials=[]), at_time=0)
    assert session._controller.navigation is retained_navigation
    assert prepared_sources == [dict(lights="new-authoritative-light")]
    assert [capture[0] for capture in captures] == [dict(lights="new-authoritative-light"), dict(materials=[])]

    # A newer gesture reserved while source acquisition is in flight wins over
    # that source's older navigation. The controller tests below also exercise
    # this ordering across a real background preparation and scene commit.
    def capture_with_newer_gesture(request):
        captures.append(request)
        newer = session._controller.reserve()
        session._controller.submit_navigation(dict(camera="newer-gesture"), newer)

    session._start_capture = capture_with_newer_gesture
    source = session.apply(dict(camera="captured-camera"), at_time=0)
    assert session._controller.navigation[0] > source
    assert session._controller.navigation[1] == dict(camera="newer-gesture")
finally:
    if old_hou is None:
        sys.modules.pop("hou", None)
    else:
        sys.modules["hou"] = old_hou

# Continuous motion may finish preparing a pose before the previous image has
# painted. Use that otherwise idle interval to replace it with a fresher pose,
# without restarting running work or discarding complete geometry snapshots.
module.ExternalBackend = Backend
package._native.navigation_info = lambda nav: dict(root_revision=nav)
package._native.navigation_snapshot = lambda *args: None  # Camera overlay is unchanged.
package._native.navigation_context_equal = lambda *args: True
package._native.navigation_basis_equal = lambda *args: True

# The production policy waits for image feedback and prepares only the newest pose.
with patch.dict(os.environ):
    os.environ.pop("CYCLES_HOUDINI_POSE_REFRESH", None)
    default_controller = module.QueuedController(None)
    start_preparation = default_controller._start_preparation
    deferred = {1: threading.Event(), 2: threading.Event()}
    default_prepared = []

    def observe_deferral(defer_poses=False):
        start_preparation(defer_poses)
        navigation = default_controller.navigation
        if defer_poses and navigation is not None and navigation[2] in deferred:
            deferred[navigation[2]].set()

    def prepare_latest(target, navigation, scene):
        if navigation is not None:
            default_prepared.append(navigation)
            return dict(target, pose=navigation), 0.0
        return target, 0.0

    default_controller._start_preparation = observe_deferral
    default_controller._prepare_owned = prepare_latest
    try:
        default_controller.set_feedback("visible-viewer", True)
        first = default_controller.submit(dict(geometry="original", pose=0))
        until(lambda: default_controller.committed == first)
        for revision, observed in deferred.items():
            submission = default_controller.reserve()
            default_controller.require_scene(submission)
            default_controller.submit_navigation(revision, submission)
            assert observed.wait(3), f"Pose {revision} did not reach the feedback gate"
            with default_controller.condition:
                assert default_controller.preparing is None and not default_prepared
        default_controller.presented(first)
        until(lambda: default_controller.applied_roots_revision == 2)
        assert default_prepared == [2] and default_controller.pose_preparation_refreshes == 0
        assert [scene["pose"] for scene, _ in default_controller.backend.scenes] == [0, 2]
    finally:
        default_controller.close()

# Exercise the separate refresh policy explicitly for stale work retirement.
controller = module.QueuedController(None)
prepared = []
retired = []
entered = threading.Event()
release = threading.Event()


class OwnedPose(dict):
    def __del__(self):
        retired.append((self["pose"], controller.condition._is_owned(), threading.current_thread().name))


def prepare_pose(target, navigation, scene):
    prepared.append((scene, navigation))
    if navigation == 3:
        entered.set()
        assert release.wait(3), "Running pose preparation was not released"
    if navigation == 9:
        raise RuntimeError("Intentional owned pose failure")
    return OwnedPose(geometry=target["geometry"], pose=navigation) if navigation is not None else target, 0.0


controller._prepare_owned = prepare_pose


def submit_pose(revision):
    submission = controller.reserve()
    controller.require_scene(submission)
    controller.submit_navigation(revision, submission)
    return submission


def ready_pose(revision):
    with controller.condition:
        return (
            controller.preparing is not None
            and controller.preparing["revision"] == revision
            and controller.preparing["future"].done()
        )


pose_refresh = patch.dict(os.environ, CYCLES_HOUDINI_POSE_REFRESH="1")
pose_refresh.start()
try:
    controller.set_feedback("visible-viewer", True)
    first = controller.submit(dict(geometry="original", pose=0))
    until(lambda: controller.committed == first)
    retained = controller.backend
    submit_pose(1)
    until(lambda: ready_pose(1))
    submit_pose(2)
    until(lambda: ready_pose(2))
    until(lambda: any(pose == 1 for pose, locked, thread in retired))
    assert all(not locked and thread != "MainThread" for pose, locked, thread in retired if pose == 1), retired
    assert controller.pose_preparation_refreshes == 1
    assert controller.committed == first and len(retained.scenes) == 1

    # Opening feedback while the fresher preparation is running must not
    # restart it yet again. It finishes and yields a preview before revision 4.
    submit_pose(3)
    assert entered.wait(3)
    controller.presented(first)
    latest = submit_pose(4)
    time.sleep(0.04)
    with controller.condition:
        assert controller.preparing["revision"] == 3
    release.set()
    until(lambda: any(scene[0]["pose"] == 3 for scene in retained.scenes))
    assert not any(scene[0]["pose"] == 4 for scene in retained.scenes)
    controller.presented(controller.committed)
    until(lambda: any(scene[0]["pose"] == 4 for scene in retained.scenes))
    assert controller.applied_roots_revision == 4 and controller.backend is retained

    # Completed source work survives newer poses and commits its geometry.
    geometry = controller.reserve()
    controller.require_scene(geometry)
    controller.submit(dict(geometry="new-geometry", pose=4), geometry)
    until(lambda: ready_pose(4))
    with controller.condition:
        owned = controller.preparing
        assert owned["scene"]
    refreshes = controller.pose_preparation_refreshes
    submit_pose(5)
    submit_pose(6)
    time.sleep(0.04)
    with controller.condition:
        assert controller.preparing is owned
    assert controller.pose_preparation_refreshes == refreshes
    controller.presented(controller.committed)
    until(lambda: any(scene[0]["geometry"] == "new-geometry" for scene in retained.scenes))
    controller.presented(controller.committed)
    until(lambda: controller.acknowledged == dict(geometry="new-geometry", pose=6))

    # A failed preparation is an internal failure, never disposable stale input.
    submit_pose(9)
    until(lambda: ready_pose(9))
    submit_pose(10)
    time.sleep(0.04)
    assert controller.pose_preparation_refreshes == refreshes
    controller.presented(controller.committed)
    until(lambda: controller.cached["status"]["state"] == "failed")
    assert "Intentional owned pose failure" in controller.cached["status"]["error"]
finally:
    release.set()
    controller.close()
    pose_refresh.stop()
assert not controller.thread.is_alive()

destination = artifacts_root() / "stage1/queued-feedback"
destination.mkdir(parents=True, exist_ok=True)
(destination / "queued-feedback-test.json").write_text(
    json.dumps(
        dict(
            result="passed",
            checks=[
                "Exact paint acknowledgement and bounded coalescing",
                "Pause/Stop bypass unavailable future passes and stalled viewers",
                "Hidden viewer, fallback timer, working-color invalidation and shutdown",
                "Superseded safe rejection keeps the newest repair reachable",
                "Current invalid scene reports rejection and repairs in the same backend",
                "Atomic display identity under concurrent publication, deduplicated wakeups and unsubscribe",
                "Pose acquisition blocks nested source loading, restores guards and drops stopped/paused/replaced work",
                "Default feedback gate defers pose preparation and coalesces to the newest pose",
                "Experimental pose refresh preserves running jobs, geometry, fairness and failures",
            ],
            scope="Deterministic controller/backend fixture; actual worker/image contracts are tested separately.",
        ),
        indent=2,
    )
)
print("Preview pacing: coalescing, exact acknowledgement, Pause/Stop, hidden viewers, fallback and shutdown passed")
