# SPDX-License-Identifier: Apache-2.0
"""The interactive renderer's session log: what it writes, what it never writes, and stall reports.

A real artist's fault (23 Nov 2026: a first-time GPU kernel compile of minutes, worker restarts, a frozen
window) left nothing to read: the worker logs hold only engine errors. These tests pin the host-side record.
"""

import io
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from cycles_houdini.ipr import queued, session_log, worker_log

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CYCLES_HOUDINI_WORKER_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("CYCLES_HOUDINI_SESSION_LOG", raising=False)
    monkeypatch.setattr(session_log, "_target", None)
    yield tmp_path
    assert session_log.flush(2)


def lines(path):
    assert session_log.flush(2)
    return path.read_text(encoding="utf-8").splitlines()


def test_one_file_per_process_with_header_time_and_renderer(log_dir):
    assert session_log.write("render session created", disk_only=False)
    assert session_log.write("worker process started", 4242, worker_log="abc.log")
    path = session_log.path()
    assert path.parent == log_dir and path.name.startswith("session-") and path.suffix == ".log"
    text = lines(path)
    assert "Cycles for Houdini session log" in text[0]
    assert text[1].endswith("[renderer -] render session created | disk_only=False")
    assert "[renderer 4242] worker process started | worker_log=abc.log" in text[2]
    # Wall-clock time with milliseconds on every line.
    assert all(line[:4].isdigit() and line[10] == " " and line[19] == "." for line in text)


def test_user_folder_is_never_written(log_dir, monkeypatch, tmp_path):
    home = tmp_path / "Users" / "artist"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERNAME", "artist")
    session_log.write("failed", error=f"cannot read {home}/x.exr and C:\\Users\\Artist\\y.exr; start art")
    line = lines(session_log.path())[-1]
    assert "artist" not in line.lower().replace("start art", "")
    assert "~/x.exr" in line and "C:\\Users\\<user>\\y.exr" in line and "start art" in line


def test_size_limit_ends_with_one_note(log_dir, monkeypatch):
    monkeypatch.setattr(session_log, "LIMIT_BYTES", 400)
    for index in range(50):
        session_log.write("stage", index, text="x" * 40)
    text = lines(session_log.path())
    assert "log limit of 400 bytes reached" in text[-1]
    assert session_log.path().stat().st_size <= 400
    assert not session_log.write("after the limit")


def test_off_switch_and_unwritable_folder_never_raise(log_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("CYCLES_HOUDINI_SESSION_LOG", "0")
    assert session_log.path() is None and not session_log.write("nothing")
    monkeypatch.setenv("CYCLES_HOUDINI_SESSION_LOG", "1")
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    monkeypatch.setenv("CYCLES_HOUDINI_WORKER_LOG_DIR", str(blocker / "below"))
    session_log.write("cannot be written")
    assert session_log.flush(2)
    assert not session_log.path().exists()
    assert not session_log.write("rejected after the I/O error")


def test_stage_key_ignores_counters():
    assert session_log.stage_key("rendering", "Sample 3/16") == session_log.stage_key("rendering", "Sample 9/16")
    assert session_log.stage_key("rendering", "Sample 3/16") != session_log.stage_key("complete", "Sample 3/16")


def test_kernel_loading_text_survives_a_queued_edit():
    engine = "Loading render kernels (may take a few minutes the first time)"
    message, original = queued.artist_message(engine, True)
    assert message.startswith(engine) and "Ambient Occlusion" in message and message.endswith("An edit is queued.")
    assert original == engine
    assert queued.artist_message(engine, False) == (
        engine + queued.KERNEL_LOADING_NOTE["Loading render kernels"],
        engine,
    )
    denoise = "Loading denoising kernels (may take a few minutes the first time)"
    assert queued.artist_message(denoise, False)[0] == denoise + ". Done once per GPU; fast afterwards."
    assert queued.artist_message("Sample 3/16", True) == ("Scene update queued", None)
    assert queued.artist_message("Sample 3/16", False) == ("Sample 3/16", None)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_stall_monitor_reports_once_then_the_return():
    clock, written = Clock(), []
    monitor = session_log.StallMonitor(
        2.0, clock=clock, writer=lambda event, **fields: written.append((event, fields)), stacks=lambda: "STACKS"
    )
    monitor.close()  # observations are driven by hand here
    monitor.beat("viewer")
    clock.now += 1.0
    assert monitor.check() == [] and written == []
    clock.now += 1.5
    assert monitor.check() == ["viewer"]
    assert written[0][0].startswith("viewer GUI thread stalled") and written[0][1]["block"] == "STACKS"
    clock.now += 5.0
    assert monitor.check() == []  # one report per stall
    monitor.beat("viewer")
    assert written[-1][0] == "viewer GUI thread was away"
    assert written[-1][1]["seconds"] == pytest.approx(7.5) and written[-1][1]["reported_while_away"] is True
    monitor.rest("viewer")
    clock.now += 10.0
    assert monitor.check() == []  # a viewer that stopped polling on purpose is not stalled
    monitor.poll_took("viewer", 0.1)
    monitor.poll_took("viewer", 0.6)
    assert [event for event, _ in written].count("viewer poll took long") == 1


def test_stall_is_recorded_while_the_polling_thread_is_still_blocked():
    reported = threading.Event()
    written = []

    def writer(event, **fields):
        written.append((event, fields))
        if event.startswith("viewer GUI thread stalled"):
            reported.set()

    monitor = session_log.StallMonitor(0.2, writer=writer)
    try:
        monitor.beat("viewer")
        blocked_until = time.monotonic() + 3.0
        # This thread stays away from its next beat; the monitor thread must report meanwhile.
        while not reported.is_set() and time.monotonic() < blocked_until:
            time.sleep(0.05)
        assert reported.is_set(), written
        block = written[0][1]["block"]
        assert block.startswith("--- main (GUI) thread")
        assert "test_stall_is_recorded_while_the_polling_thread_is_still_blocked" in block
    finally:
        monitor.close()


def test_watchdog_reports_the_worker_it_stopped():
    from cycles_houdini.ipr.exchange_watchdog import ExchangeWatchdog

    class Process:
        killed = False

        def kill(self):
            self.killed = True

    process, expired = Process(), threading.Event()
    guard = ExchangeWatchdog(process, on_expire=expired.set)
    try:
        guard.arm(0.05)
        assert expired.wait(2) and process.killed
    finally:
        guard.close()
    failing = ExchangeWatchdog(Process(), on_expire=lambda: 1 / 0)
    try:
        failing.arm(0.02)
        time.sleep(0.2)
        assert failing.thread.is_alive(), "a failing report must not end the monitor"
    finally:
        failing.close()


def test_a_viewer_that_stopped_polling_on_purpose_writes_no_away_line():
    clock, written = Clock(), []
    monitor = session_log.StallMonitor(2.0, clock=clock, writer=lambda event, **fields: written.append(event))
    monitor.close()
    monitor.beat("viewer")
    monitor.rest("viewer")  # hidden: the timer stopped on purpose
    clock.now += 3600.0  # an idle evening
    assert monitor.check() == []
    monitor.beat("viewer")  # shown again
    assert written == []


def test_stall_reports_are_bounded_per_session():
    clock, written = Clock(), []
    monitor = session_log.StallMonitor(
        2.0, clock=clock, writer=lambda event, **fields: written.append((event, fields)), stacks=lambda: "S"
    )
    monitor.close()
    for _ in range(session_log.STALL_REPORTS + 5):
        monitor.beat("viewer")
        clock.now += 3.0
        monitor.check()
    stalls = [fields for event, fields in written if event.startswith("viewer GUI thread stalled")]
    assert len(stalls) == session_log.STALL_REPORTS + 5
    assert sum("block" in fields for fields in stalls) == session_log.STALL_REPORTS


def test_main_thread_stack_comes_first_and_whole_among_many_threads(log_dir):
    release = threading.Event()
    started = threading.Barrier(31)

    def idle():
        started.wait()
        release.wait(10)

    others = [threading.Thread(target=idle, name=f"other-{index}") for index in range(30)]
    for thread in others:
        thread.start()
    try:
        started.wait()
        stacks = session_log.python_stacks()
        assert session_log.write("viewer GUI thread stalled; Python stacks follow", seconds=2.5, block=stacks)
    finally:
        release.set()
        for thread in others:
            thread.join()
    first = stacks.split("\n--- ")[0]
    assert first.startswith("--- main (GUI) thread")
    assert "test_main_thread_stack_comes_first_and_whole_among_many_threads" in first
    assert "stacks = session_log.python_stacks()" in first  # the innermost frame, in full
    assert "more threads not shown" in stacks and len(stacks) <= session_log.STACK_BYTES + 2000
    assert session_log.flush(2)
    text = session_log.path().read_text(encoding="utf-8")
    assert "\n    --- main (GUI) thread" in text and first.splitlines()[-1].strip() in text


def test_header_names_the_installation(log_dir, monkeypatch, tmp_path):
    home = tmp_path / "Users" / "artist"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERNAME", "artist")
    monkeypatch.setenv("CYCLES_HOUDINI_RELEASE_ID", "builder-de0ec4-24afd9c8")
    monkeypatch.setenv("CYCLES_HOUDINI_PACKAGE_FILE", str(home / "CyclesForHoudini-builder.json"))
    session_log.write("render session created")
    header = lines(session_log.path())[0]
    assert "Houdini " in header and "Cycles for Houdini builder-de0ec4-24afd9c8" in header
    assert "CyclesForHoudini-builder.json" in header
    assert "artist" not in header.lower() and "~" in header


@pytest.mark.parametrize("outcome", ["complete", "cancelled", "failed"])
def test_motion_capture_stage_reports_begin_and_end(monkeypatch, outcome):
    records = []
    monkeypatch.setattr(session_log, "write", lambda event, **fields: records.append((event, fields)))

    class NativeInterrupt(RuntimeError):
        pass

    class HostInterrupt(RuntimeError):
        pass

    label = []

    class Operation:
        def __init__(self, text):
            label.append(text)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def updateProgress(self):
            label.append("checked")

    monkeypatch.setitem(
        sys.modules,
        "hou",
        SimpleNamespace(
            isUIAvailable=lambda: True, InterruptableOperation=Operation, OperationInterrupted=HostInterrupt
        ),
    )
    module = "cycles_houdini.scene.instancing"
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(nested_requests=iter))
    monkeypatch.setattr(queued, "_native", SimpleNamespace(MotionCaptureInterrupted=NativeInterrupt))
    update = dict(motion=dict(enabled=True, shutter_frames=0.5), geometry=[dict(motion="deformation")])

    def capture():
        with queued._motion_capture_stage(update, "shutter samples", 12, interactive=True):
            if outcome == "cancelled":
                raise NativeInterrupt()
            if outcome == "failed":
                raise ValueError("shader failed")

    if outcome == "complete":
        capture()
    else:
        expected = queued._CaptureInterrupted if outcome == "cancelled" else ValueError
        with pytest.raises(expected):
            capture()
    assert label == ["Reading the scene for motion blur", "checked"]
    assert records[0] == ("motion blur acquisition began", dict(stage="shutter samples", submission=12))
    assert records[1][0] == "motion blur acquisition ended"
    assert records[1][1]["result"] == outcome and records[1][1]["seconds"] >= 0

    with queued._motion_capture_stage(dict(geometry=update["geometry"]), "scene roots", 13, interactive=True):
        pass
    assert len(records) == 2


def test_busy_session_storage_does_not_block_producers(log_dir, monkeypatch):
    blocked, release = threading.Event(), threading.Event()
    original_open = Path.open

    def slow_open(path, *args, **kwargs):
        if path == session_log.path():
            blocked.set()
            assert release.wait(3)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)
    assert session_log.write("first")
    try:
        assert blocked.wait(2)
        assert all(session_log.write("queued") for _ in range(session_log._QUEUE_LENGTH))
        assert not session_log.write("dropped")
    finally:
        release.set()
        # The queue is full; give the final barrier time to enter it.
        assert session_log.flush(2)
    text = lines(session_log.path())
    assert any("dropped 1 log records" in line for line in text)
    assert sum(line.endswith("queued") for line in text) == session_log._QUEUE_LENGTH


def test_worker_stderr_drains_when_disk_is_slow_and_never_grows_past_limit(tmp_path, monkeypatch):
    blocked, release = threading.Event(), threading.Event()
    original_open = Path.open
    path = tmp_path / ("a" * 32 + ".log")
    monkeypatch.setattr(worker_log, "LIMIT_BYTES", 1024)

    def slow_open(target, *args, **kwargs):
        if target == path and args == ("xb",):
            blocked.set()
            assert release.wait(3)
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)
    log = worker_log.WorkerLog(path, io.BytesIO(b"x" * 65536))
    try:
        assert blocked.wait(2) and log.drained.wait(2)
        assert not log.write_stopped.is_set()
    finally:
        release.set()
        log.finish(2)
    assert len(path.read_bytes()) <= 1024
    assert b"worker log omitted" in path.read_bytes()


def test_log_types_have_independent_retention(log_dir, monkeypatch):
    monkeypatch.setattr(session_log, "_RETAIN", 2)
    monkeypatch.setattr(worker_log, "RETAIN", 2)
    for index in range(3):
        (log_dir / f"{index:032x}.log").write_text("old worker")
        (log_dir / f"session-old-{index}.log").write_text("old session")
    assert session_log.write("new session")
    assert session_log.flush(2)
    assert len(list(log_dir.glob("session-*.log"))) == 2
    assert len([p for p in log_dir.glob("*.log") if not p.name.startswith("session-")]) == 3

    path = log_dir / ("a" * 32 + ".log")
    log = worker_log.WorkerLog(path, io.BytesIO(b"new worker"))
    log.finish(2)
    assert path.read_bytes() == b"new worker"
    assert len(list(log_dir.glob("session-*.log"))) == 2
    assert len([p for p in log_dir.glob("*.log") if not p.name.startswith("session-")]) == 2
