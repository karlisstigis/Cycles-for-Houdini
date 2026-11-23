# SPDX-License-Identifier: Apache-2.0
"""A private text record of what the interactive renderer did when something goes wrong.

One file per Houdini process, beside the worker logs (an installed copy: <install>\\data\\artifacts\\workers,
named session-<date>-<time>-<pid>.log, with separate retention). One line per event:
local time, the renderer's process id, what happened. Recorded: a worker start and its devices, every change of
the render state or of the renderer's stage text (digits ignored, so the sample counter adds no lines), replies
that took longer than SLOW_EXCHANGE_SECONDS, failures, watchdog stops, and stalls of the viewer's GUI thread
(with the Python stacks of all threads, once per stall). A bounded background writer flushes each accepted
record and may drop records under load. Treat this log as private. Free-form errors, stage messages and
Python stacks can contain paths, node names and other project details. Review it before sharing.
CYCLES_HOUDINI_SESSION_LOG=0 turns it off.
"""

import atexit
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

LIMIT_BYTES = 4 * 1024 * 1024
SLOW_EXCHANGE_SECONDS = 2.0
STALL_SECONDS = 2.0
# A stall report: the GUI (main) thread first, then the others; bounded so one bad session cannot fill the log.
STACK_FRAMES = 12
STACK_THREADS = 20
STACK_BYTES = 16 * 1024
STALL_REPORTS = 20
_RETAIN = 16
_QUEUE_LENGTH = 128
_MAX_LINE_BYTES = 4096
_lock = threading.Lock()
_target = None
_pending = queue.Queue(maxsize=_QUEUE_LENGTH)
_writer = None
_digits = re.compile(r"\d+(\.\d+)?")


def enabled():
    return os.environ.get("CYCLES_HOUDINI_SESSION_LOG", "1") != "0"


def _directory():
    explicit = os.environ.get("CYCLES_HOUDINI_WORKER_LOG_DIR")
    if explicit:
        return Path(explicit)
    root = os.environ.get("CYCLES_HOUDINI_SOURCE_ROOT")
    return Path(root) / "artifacts/workers" if root else None


def _current_target():
    global _target
    if not enabled():
        return None
    directory = _directory()
    if directory is None:
        return None
    with _lock:
        if _target is None or _target.path.parent != directory:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            _target = _LogFile(directory / f"session-{stamp}-{os.getpid()}.log")
        return _target


def path():
    """This process's log path, or None when logging is off or has no folder."""
    target = _current_target()
    return target.path if target else None


def stage_key(state, message):
    """The part of a status that is worth a line: the state and the stage text without its numbers."""
    return str(state), _digits.sub("#", str(message or ""))[:200]


def _identity():
    """Houdini version and Cycles for Houdini build, so a log sent alone identifies the installation."""
    houdini = None
    hou = sys.modules.get("hou")
    if hou is not None and threading.current_thread() is threading.main_thread():
        try:
            houdini = hou.applicationVersionString()
        except Exception:
            houdini = None
    return (
        f"Houdini {houdini or os.environ.get('HOUDINI_VERSION') or 'unknown'}; "
        f"Cycles for Houdini {os.environ.get('CYCLES_HOUDINI_RELEASE_ID') or 'development build'}"
        + (f" ({os.environ['CYCLES_HOUDINI_PACKAGE_FILE']})" if os.environ.get("CYCLES_HOUDINI_PACKAGE_FILE") else "")
    )


def prune_logs(path, belongs, keep):
    """Prune one kind of log. On Windows an open log cannot be unlinked."""
    try:
        others = [
            (file.stat().st_mtime_ns, file) for file in path.parent.glob("*.log") if file != path and belongs(file)
        ]
        others.sort(reverse=True)
        for _, file in others[max(0, keep - 1) :]:
            try:
                file.unlink()
            except OSError:
                pass
    except OSError:
        pass


class _LogFile:
    """One file. Disk state and streams belong exclusively to the writer."""

    def __init__(self, path):
        self.path = path
        self.full = False
        self.header = None
        self.dropped = 0

    def append(self, stream, stamp, data):
        if self.full:
            return
        limit_note = f"{stamp} log limit of {LIMIT_BYTES} bytes reached; nothing more is written\n".encode()
        if stream.tell() == 0:
            header = _prefix(self.header, max(0, LIMIT_BYTES - len(limit_note)))
            stream.write(header)
        with _lock:
            dropped, self.dropped = self.dropped, 0
        if dropped:
            data = f"{stamp} dropped {dropped} log records while the log writer was busy\n".encode() + data
        if stream.tell() + len(data) + len(limit_note) > LIMIT_BYTES:
            data = limit_note[: max(0, LIMIT_BYTES - stream.tell())]
            self.full = True
        stream.write(data)
        stream.flush()


def _serve():
    active = stream = None
    while True:
        target, stamp, data = _pending.get()
        try:
            if target is None:
                data.set()  # Barrier used for short, best-effort shutdown.
                continue
            if target.full:
                continue
            if target is not active:
                previous, stream = stream, None
                active = None
                if previous is not None:
                    try:
                        previous.close()
                    except OSError:
                        pass
                target.path.parent.mkdir(parents=True, exist_ok=True)
                stream = target.path.open("ab")
                active = target
                prune_logs(target.path, lambda other: other.name.startswith("session-"), _RETAIN)
            target.append(stream, stamp, data)
        except (OSError, ValueError):
            target.full = True
        finally:
            _pending.task_done()


def flush(timeout=0.25):
    """Wait briefly for already accepted records, for shutdown and tests only."""
    if _writer is None:
        return True
    deadline = time.monotonic() + timeout
    complete = threading.Event()
    try:
        _pending.put((None, None, complete), timeout=timeout)
    except queue.Full:
        return False
    return complete.wait(max(0, deadline - time.monotonic()))


atexit.register(flush)


def write(event, renderer=None, block=None, **fields):
    """Queue a bounded record without filesystem access. Never raises."""
    global _writer
    try:
        target = _current_target()
        if target is None or target.full:
            return False
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
        details = " ".join(f"{key}={_text(value)}" for key, value in fields.items() if value is not None)
        line = f"{stamp} [renderer {renderer if renderer is not None else '-'}] {_text(event)}"
        line = _scrub((line + " | " + details if details else line).replace("\r", " "))
        data = _prefix(line, _MAX_LINE_BYTES) + b"\n"
        if block:
            stack = "".join("    " + part + "\n" for part in _scrub(str(block)[:STACK_BYTES]).splitlines())
            data += _prefix(stack, STACK_BYTES - 1).rstrip(b"\n") + b"\n"
        header = None
        if target.header is None:
            identity = _scrub(_text(_identity())).replace("\r", " ")
            header = f"{stamp} Cycles for Houdini session log; Houdini process {os.getpid()}; {identity}\n"
        with _lock:
            if target.header is None:
                target.header = header
            if _writer is None:
                _writer = threading.Thread(target=_serve, name="Cycles session log writer", daemon=True)
                _writer.start()
            try:
                _pending.put_nowait((target, stamp, data))
            except queue.Full:
                target.dropped += 1
                return False
        return True
    except Exception:
        return False


def _prefix(text, size):
    """Keep a byte cap without splitting a UTF-8 character."""
    return text.encode("utf-8", "replace")[:size].decode("utf-8", "ignore").encode("utf-8")


def _scrub(text):
    """Shorten home paths and redact the current account name in common home folder forms."""
    home = os.path.expanduser("~")
    if home and home != "~":
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user:
        # Only as a folder name (C:\\Users\\<name>, /home/<name>): a short name must not change other words.
        text = re.sub(r"(?i)([\\/](?:users|home)[\\/])" + re.escape(user) + r"(?=[\\/]|$|\s)", r"\1<user>", text)
    return text


def _text(value):
    if isinstance(value, float):
        return f"{value:.3f}"
    text = str(value).replace("\n", " / ")
    return text if len(text) <= 500 else text[:497] + "..."


def python_stacks(frames=STACK_FRAMES, threads=STACK_THREADS, limit=STACK_BYTES):
    """The Python stacks of the main (GUI) thread first, then the other threads, innermost call last."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    main = threading.main_thread().ident
    current = sys._current_frames()
    order = sorted(current, key=lambda ident: ident != main)
    parts, size = [], 0
    for count, ident in enumerate(order):
        if count >= threads:
            parts.append(f"--- {len(order) - count} more threads not shown")
            break
        stack = "".join(traceback.format_stack(current[ident], limit=frames)).rstrip("\n")
        label = "main (GUI) thread" if ident == main else f"thread {names.get(ident, ident)}"
        part = f"--- {label}\n{stack}"
        if parts and size + len(part) > limit:
            parts.append(f"--- {len(order) - count} more threads not shown (size limit)")
            break
        parts.append(part)
        size += len(part)
    return "\n".join(parts)


class StallMonitor:
    """Reports when a viewer's GUI-thread poll stops arriving while it is expected.

    The viewer calls beat(key) at the start of each poll and rest(key) whenever it stops polling on
    purpose (hidden, closed). A background thread notices a beat older than STALL_SECONDS and writes one
    line with the Python stacks of all threads; the next beat writes how long the thread was away. A
    stall the background thread could not observe (it needs the Python lock too) is still reported by the
    next beat. poll_took(key, seconds) records a single poll that itself took long.
    """

    def __init__(self, stall_seconds=STALL_SECONDS, clock=time.monotonic, writer=None, stacks=python_stacks):
        self.stall_seconds = stall_seconds
        self.clock = clock
        self.writer = writer or write
        self.stacks = stacks
        self.beats = {}
        self.reported = set()
        self.reports = 0
        self.condition = threading.Condition()
        self.thread = None
        self.closed = False

    def beat(self, key):
        now = self.clock()
        with self.condition:
            previous = self.beats.get(key)
            self.beats[key] = now
            reported = key in self.reported
            self.reported.discard(key)
            if self.thread is None and not self.closed:
                self.thread = threading.Thread(target=self._run, name="Cycles viewer stall monitor", daemon=True)
                self.thread.start()
        if previous is not None and now - previous >= self.stall_seconds:
            self.writer("viewer GUI thread was away", seconds=now - previous, reported_while_away=reported)

    def rest(self, key):
        with self.condition:
            self.beats.pop(key, None)
            self.reported.discard(key)

    def poll_took(self, key, seconds):
        if seconds >= self.stall_seconds / 4:
            self.writer("viewer poll took long", seconds=seconds)

    def check(self):
        """One observation (the background thread calls this every half stall period)."""
        now = self.clock()
        with self.condition:
            late = [
                key for key, last in self.beats.items() if now - last >= self.stall_seconds and key not in self.reported
            ]
            self.reported.update(late)
            ages = {key: now - self.beats[key] for key in late}
        for key in late:
            self.reports += 1
            if self.reports <= STALL_REPORTS:
                self.writer("viewer GUI thread stalled; Python stacks follow", seconds=ages[key], block=self.stacks())
            else:
                self.writer("viewer GUI thread stalled (no more stacks this session)", seconds=ages[key])
        return late

    def _run(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                self.condition.wait(self.stall_seconds / 2)
                if self.closed:
                    return
            self.check()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


_monitor = None


def stall_monitor():
    global _monitor
    if _monitor is None:
        _monitor = StallMonitor()
    return _monitor
