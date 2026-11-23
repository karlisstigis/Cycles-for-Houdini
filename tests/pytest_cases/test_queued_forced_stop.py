# SPDX-License-Identifier: Apache-2.0
"""Closing a session whose worker is busy: our own forced stop is not a renderer failure.

QueuedController.close() waits a grace period for the controller thread, then
kills a worker that has not answered the shutdown command (it can be inside a
long scene update or BVH build it cannot interrupt). The interrupted shutdown
exchange must not be reported as "Renderer shutdown failed". A worker that
dies on its own during shutdown is still reported.
"""

import threading
from types import SimpleNamespace

import pytest

from cycles_houdini.ipr import queued

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

CRASH = 3221225477  # STATUS_ACCESS_VIOLATION


class Stdin:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        self.process.written.extend(data)
        if self.process.crash_on_shutdown and len(self.process.written) >= queued._HEADER.size:
            self.process.exit(CRASH)

    def flush(self):
        pass

    def close(self):
        pass


class Stdout:
    def __init__(self, process):
        self.process = process

    def read(self, count):
        self.process.reading.set()
        assert self.process.ended.wait(10), "fake worker never ended"
        return b""

    def close(self):
        pass


class Process:
    """A worker that never answers the shutdown command, or crashes on it.

    As on Windows, poll() reports a killed process as still running until it
    is waited for: TerminateProcess is asynchronous.
    """

    pid = 4242

    def __init__(self, *, crash_on_shutdown=False):
        self.crash_on_shutdown = crash_on_shutdown
        self.written = bytearray()
        self.reading, self.ended = threading.Event(), threading.Event()
        self.code = None
        self.kills = 0
        self.stdin, self.stdout = Stdin(self), Stdout(self)

    def exit(self, code):
        self.code = code
        self.ended.set()

    def poll(self):
        return self.code

    def kill(self):
        self.kills += 1
        self.ended.set()

    def wait(self, timeout=None):
        assert self.ended.wait(timeout)
        if self.code is None:
            self.code = 1
        return self.code


def idle_reply():
    return dict(success=True, status=dict(state="rendering", generation=0, error="", message=""), frame=None)


def backend(process):
    result = object.__new__(queued.ExternalBackend)
    result.input = None  # Explicit legacy mapping transport; exercise the real shutdown exchange.
    result.request = 0
    result.last_snapshot = None
    result.metrics = dict(
        input_bytes=0, output_bytes=0, encode_seconds=0.0, decode_seconds=0.0, mapping_seconds=0.0, exchanges=0
    )
    result.process = process
    result.watchdog = SimpleNamespace(arm=lambda **_: None, disarm=lambda: None, close=lambda: None, extensions=0)
    result.log_path = "worker.log"
    result.log = SimpleNamespace(finish=lambda: None)
    result.poll = idle_reply
    return result


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(
        queued, "_native", SimpleNamespace(status=lambda capture: dict(state="idle", error="", message=""))
    )
    made = []

    def make(process):
        result = queued.QueuedController(None)
        result.backend = backend(process)
        original_join = result.thread.join
        joins = []

        def short_grace(timeout=None):
            # The product waits 5 s; the first join is the grace period.
            joins.append(timeout)
            return original_join(0.2 if len(joins) == 1 and timeout is not None else timeout)

        result.thread.join = short_grace
        made.append((result, process))
        return result

    yield make
    for result, process in made:
        process.ended.set()
        result.thread.join(5)


def test_forced_stop_of_a_busy_worker_is_not_a_failure(controller):
    process = Process()
    session = controller(process)
    session.close()
    assert process.reading.is_set(), "the shutdown command never reached the worker"
    assert process.kills >= 1
    assert not session.thread.is_alive()
    assert session.shutdown_error == ""
    status = session.status()
    assert status["state"] == "closed" and not status["error"], status
    assert status["device_allocation_bytes"] == 0 and status["owned_geometry_bytes"] == 0


def test_worker_crash_during_shutdown_is_still_reported(controller):
    process = Process(crash_on_shutdown=True)
    session = controller(process)
    with pytest.raises(RuntimeError, match=r"Renderer shutdown failed: Cycles worker disconnected \(exit 3221225477\)"):
        session.close()
    assert process.kills == 0
    assert session.status()["state"] == "failed"


class ReadyBackend:
    def __init__(self, process=None):
        self.process = process
        self.closed = False
        self.applied = threading.Event()

    def apply(self, desired, acknowledged):
        self.applied.set()
        reply = idle_reply()
        reply.update(renderer_pid=4242)
        return reply

    def poll(self):
        return self.apply(None, None)

    def close(self):
        self.closed = True
        return dict(state="closed", error="")

    abort = close


@pytest.fixture
def launch_fixture(monkeypatch):
    monkeypatch.setattr(
        queued,
        "_native",
        SimpleNamespace(
            status=lambda _: dict(state="idle", error="", message=""),
            snapshot_color_identity=lambda desired: desired,
        ),
    )


def test_close_cancels_replacement_launch(monkeypatch, launch_fixture):
    standby = ReadyBackend()
    launched = threading.Event()
    process = Process()

    def launch(_capture, desired, *, on_start=None):
        if desired == "old colors":
            return standby
        if on_start is not None:
            on_start(process)
        launched.set()
        assert process.ended.wait(2), "replacement worker was not stopped"
        raise RuntimeError("worker shut down during startup")

    monkeypatch.setattr(queued, "ExternalBackend", launch)
    controller = queued.QueuedController(None, startup_snapshot="old colors")
    try:
        controller.submit("new colors")
        assert launched.wait(2) and standby.closed
        controller.close(wait=False)
        assert process.ended.wait(0.2), "nonblocking close did not stop the starting worker"
        controller.close()
        assert not controller.thread.is_alive()
        assert not controller.shutdown_error
        assert controller.status()["state"] == "closed"
    finally:
        process.ended.set()
        controller.close()


def test_failed_start_recovers_on_the_next_submission(monkeypatch, launch_fixture):
    first = Process()
    ready = ReadyBackend()
    failed = threading.Event()

    def launch(_capture, desired, *, on_start):
        if desired == "initial":
            on_start(first)
            raise RuntimeError("SDK provider timed out")
        return ready

    monkeypatch.setattr(queued, "ExternalBackend", launch)
    report = queued.session_log.write

    def report_failure(event, *args, **fields):
        if event.startswith("renderer failed;"):
            failed.set()
        return report(event, *args, **fields)

    monkeypatch.setattr(queued.session_log, "write", report_failure)
    controller = queued.QueuedController(None, startup_snapshot="initial")
    try:
        controller.submit("initial")
        assert failed.wait(2)
        with controller.condition:
            assert controller.condition.wait_for(lambda: controller.cached["status"]["state"] == "failed", timeout=2)
            assert controller.cached["status"]["error"] == "SDK provider timed out"
        controller.submit("retry")
        assert ready.applied.wait(2)
    finally:
        controller.close()
