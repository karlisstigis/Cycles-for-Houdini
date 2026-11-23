# SPDX-License-Identifier: Apache-2.0
"""Renderer ownership survives coalescing, rejected captures and async retirement."""

from concurrent.futures import Future
import gc
import threading
from types import SimpleNamespace
import sys
import weakref

import pytest

import cycles_houdini as ch
from cycles_houdini.blender import prototype_leases as leases
from cycles_houdini.ipr import queued

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class Assets:
    def __init__(self):
        self.pins, self.events = {}, []

    def retain_consumer(self, owner, keys):
        assert threading.current_thread() is threading.main_thread()
        self.pins[owner] = set(keys)
        self.events.append(("retain", owner, set(keys)))

    def release_consumer(self, owner):
        assert threading.current_thread() is threading.main_thread()
        self.pins.pop(owner, None)
        self.events.append(("release", owner))

    def release_capture(self, rop):
        assert self.pins  # Transfer always establishes ownership first.
        self.events.append(("capture", rop))


class Owner:
    pass


@pytest.fixture
def assets(monkeypatch):
    backend = Assets()
    monkeypatch.setattr(leases, "_assets", lambda: backend)
    monkeypatch.setitem(sys.modules, "hou", SimpleNamespace(time=lambda: 0.0, isUIAvailable=lambda: False))
    yield backend
    gc.collect()
    leases.drain_releases()


def meta(*keys, rop=7):
    return dict(rop=rop, keys=list(keys))


def pinned(backend, consumer):
    return backend.pins.get(consumer.token, set())


def test_two_sessions_with_same_rop_keep_independent_samples(assets):
    a, b = Owner(), Owner()
    first, second = leases.session_leases(a), leases.session_leases(b)
    first.begin(1, meta("old"))
    first.commit(1)
    second.begin(1, meta("new"))
    second.commit(1)
    assert first.token != second.token
    assert pinned(assets, first) == {"old"}
    assert pinned(assets, second) == {"new"}
    first.close()
    assert pinned(assets, second) == {"new"}


def test_accepted_active_pending_union_and_camera_merge(assets):
    owner = Owner()
    consumer = leases.session_leases(owner)
    consumer.begin(1, meta("accepted"))
    consumer.commit(1)
    consumer.begin(2, meta("active"))
    consumer.begin(3, meta("pending"))
    consumer.begin(4, None)
    consumer.inherit(4, 3)
    consumer.drop(3)
    assert pinned(assets, consumer) == {"accepted", "active", "pending"}
    consumer.begin(5, meta("replacement"))
    consumer.drop(4)
    assert pinned(assets, consumer) == {"accepted", "active", "replacement"}
    consumer.commit(2)
    assert pinned(assets, consumer) == {"active", "replacement"}
    consumer.drop(5)
    assert pinned(assets, consumer) == {"active"}


def test_gc_only_queues_release_and_host_drains_it(assets):
    owner = Owner()
    consumer = leases.session_leases(owner)
    consumer.begin(1, meta("mesh"))
    consumer.commit(1)
    token, ref = consumer.token, weakref.ref(owner)
    del owner
    gc.collect()
    assert ref() is None
    assert assets.pins[token] == {"mesh"}
    leases.drain_releases()
    assert token not in assets.pins


def test_native_apply_strips_ledger_and_retains_old_on_failure(assets, monkeypatch):
    received = []
    native = SimpleNamespace(
        apply=lambda handle, update, *args: received.append(update) or 3,
        close=lambda handle: None,
        cancel=lambda handle: None,
        fixture=lambda handle: 9,
    )
    monkeypatch.setattr(ch, "_native", native)
    session = object.__new__(ch.Session)
    session._handle = object()
    update = dict(geometry=[], _blender_prototypes=meta("old"))
    assert session.apply(update) == 3
    assert "_blender_prototypes" not in received[-1] and "_blender_prototypes" in update
    consumer = leases.session_leases(session)
    assert pinned(assets, consumer) == {"old"}

    def fail(*args):
        raise RuntimeError("native rejected update")

    native.apply = fail
    with pytest.raises(RuntimeError, match="rejected"):
        session.apply(dict(geometry=[], _blender_prototypes=meta("failed")))
    assert pinned(assets, consumer) == {"old"}
    session.calibration()
    assert not pinned(assets, consumer)
    session.close()
    assert consumer.token not in assets.pins


class Controller:
    def __init__(self):
        self.condition = threading.RLock()
        self.cancelled_through = 0
        self.requested = 0
        self.acknowledged = self.navigation = None
        self.closing = self.closed = False
        self.submissions = []

    def reserve(self):
        self.requested += 1
        return self.requested

    def require_scene(self, *args, **kwargs):
        pass

    def reject_capture(self, *args):
        pass

    def submit(self, desired, submission=None):
        self.submissions.append((desired, submission))
        return submission

    def control(self, command):
        if command == "cancel":
            self.cancelled_through = self.requested

    def close(self, **kwargs):
        self.closing = True


class Conversion:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, **kwargs):
        pass


def worker(monkeypatch):
    session = object.__new__(queued.WorkerSession)
    session._handle = object()
    session._controller = Controller()
    session._capture_failure = ""
    session._capture_closed = session._in_host_capture = False
    session._active_capture = session._pending_capture = None
    session._capture_events = []
    session._capture_coalesced = 0
    session.material_paths_revision = 0
    session._interactive = True
    session._conversion = Conversion()
    session.notify_color_configuration = lambda: None
    session.close_native_display = lambda: None
    session._retirement = session._capture_retirement = None
    session._retirement_done = False
    native = SimpleNamespace(
        validate_update_metadata=lambda update: None,
        acquire_images=lambda job: False,
        finish_apply=lambda job: {"scene": "completed"},
        discard_apply=lambda job: 0.0,
        close=lambda handle: None,
        retire_capture=lambda handle: object(),
    )
    monkeypatch.setattr(queued, "_native", native)
    return session, native


def active(submission, stage="convert", future=None):
    future = future or Future()
    if not future.done():
        future.set_result(0.0)
    return dict(
        job=object(),
        future=future,
        submission=submission,
        stage=stage,
        request=({"geometry": []}, 0.0, False, submission),
        acquisition=0.0,
        scan=0.0,
        preparation=0.0,
        preparation_max=0.0,
        scan_batches=0,
        preparation_yields=0,
    )


def test_worker_pending_camera_merge_and_superseded_scene_release(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(0, meta("accepted"))
    consumer.commit(0)
    consumer.begin(1, meta("active"))
    session._controller.requested = 1
    session._active_capture = active(1)
    # Prevent eager pumping from completing the artificial active transaction.
    session._pump_capture = lambda: None
    checked = []
    native.validate_update_metadata = lambda payload: checked.append(payload)
    pending = session.apply(dict(geometry=[], _blender_prototypes=meta("pending")))
    camera = session.apply(dict(camera={}))
    assert "_blender_prototypes" not in checked[0]
    assert pending not in consumer.requests and consumer.requests[camera] == {"pending"}
    assert pinned(assets, consumer) == {"accepted", "active", "pending"}
    replacement = session.apply(dict(geometry=[], _blender_prototypes=meta("replacement")))
    assert camera not in consumer.requests and consumer.requests[replacement] == {"replacement"}
    assert pinned(assets, consumer) == {"accepted", "active", "replacement"}


def test_worker_failed_and_cancelled_capture_holds_until_discard(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(0, meta("accepted"))
    consumer.commit(0)
    consumer.begin(1, meta("active"))
    consumer.begin(2, meta("pending"))
    session._controller.requested = 2
    session._active_capture = active(1)
    session._pending_capture = ({}, 0.0, False, 2)
    session.cancel()
    assert pinned(assets, consumer) == {"active"}
    session._advance_capture()
    assert session._active_capture["stage"] == "discard"
    assert pinned(assets, consumer) == {"active"}
    session._advance_capture()
    assert session._active_capture is None
    assert not pinned(assets, consumer)


def test_worker_commit_only_after_finish_and_submit_success(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(0, meta("accepted"))
    consumer.commit(0)
    consumer.begin(1, meta("failed"))
    session._active_capture = active(1)

    def fail(desired, submission):
        assert pinned(assets, consumer) == {"accepted", "failed"}
        raise RuntimeError("submission failed")

    session._controller.submit = fail
    session._advance_capture()
    assert session._active_capture["stage"] == "discard"
    assert consumer.accepted == {"accepted"}
    assert pinned(assets, consumer) == {"accepted", "failed"}
    session._advance_capture()
    assert pinned(assets, consumer) == {"accepted"}
    consumer.begin(2, meta("new"))
    session._active_capture = active(2)
    session._controller.submit = lambda *args: None
    session._advance_capture()
    assert consumer.accepted == {"new"}
    assert pinned(assets, consumer) == {"new"}


def test_worker_close_releases_only_after_host_capture_retirement(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(1, meta("accepted"))
    consumer.commit(1)

    def retire(handle):
        assert pinned(assets, consumer) == {"accepted"}
        return object()

    def close(handle):
        assert pinned(assets, consumer) == {"accepted"}

    native.retire_capture, native.close = retire, close
    session._close(wait=True, exiting=False)
    assert consumer.token not in assets.pins
    assert session._retirement_done


def test_async_retirement_failure_keeps_lease_until_native_close_succeeds(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(1, meta("accepted"))
    consumer.commit(1)
    session._retirement = Future()
    session._retirement.set_result(None)

    def fail(handle):
        raise RuntimeError("host close failed")

    native.close = fail
    with pytest.raises(RuntimeError, match="host close"):
        session._poll_retirement()
    assert pinned(assets, consumer) == {"accepted"}
    native.close = lambda handle: None
    session._poll_retirement()
    assert consumer.token not in assets.pins


@pytest.mark.parametrize("failure", ["validation", "acquisition"])
def test_worker_failed_admission_drops_only_new_samples(assets, monkeypatch, failure):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(0, meta("accepted"))
    consumer.commit(0)

    def fail(*args):
        raise RuntimeError("intentional " + failure)

    if failure == "validation":
        native.validate_update_metadata = fail
    else:
        native.acquire_apply = fail
    with pytest.raises(RuntimeError, match=failure):
        session.apply(dict(geometry=[], _blender_prototypes=meta("failed")))
    assert not consumer.requests
    assert pinned(assets, consumer) == {"accepted"}


def test_failed_discard_holds_active_but_drops_unstarted_pending(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(1, meta("active"))
    consumer.begin(2, meta("pending"))
    failed = Future()
    failed.set_exception(RuntimeError("discard failed"))
    session._active_capture = active(1, stage="discard", future=failed)
    session._pending_capture = ({}, 0.0, False, 2)
    session._advance_capture()
    assert session._capture_failure.startswith("Scene cleanup failed")
    assert pinned(assets, consumer) == {"active"}
    assert session._pending_capture is None


def test_worker_status_exposes_host_point_instance_metrics(assets, monkeypatch):
    session, native = worker(monkeypatch)
    session._pump_capture = lambda: None
    values = dict(
        cooking_seconds=0,
        extraction_seconds=0,
        extractions=0,
        cache_hits=0,
        packed_instances=0,
        index_instance_ids=0,
        point_instance_syntheses=3,
        point_instance_cache_hits=7,
        point_instances_synthesized=23,
        point_instance_seconds=0.125,
    )
    native.status = lambda handle: values
    native.native_stats = lambda handle: {}
    session._controller.status = lambda: dict(state="rendering", coalesced_updates=0)
    session.last_capture_seconds = 0
    status = session.status()
    for name in (
        "point_instance_syntheses",
        "point_instance_cache_hits",
        "point_instances_synthesized",
        "point_instance_seconds",
    ):
        assert status[name] == values[name]


def test_pending_merge_failure_releases_unaccepted_samples(assets, monkeypatch):
    session, native = worker(monkeypatch)
    consumer = leases.session_leases(session)
    consumer.begin(1, meta("active"))
    consumer.begin(2, meta("pending"))
    session._active_capture = active(1)
    session._pending_capture = ({"materials": [{"key": "old"}]}, 0.0, False, 2)
    session._controller.requested = 2
    with pytest.raises(KeyError, match="key"):
        session.apply(dict(materials=[dict(kind="emission")], _blender_prototypes=meta("invalid")))
    assert set(consumer.requests) == {1, 2}
    assert pinned(assets, consumer) == {"active", "pending"}
