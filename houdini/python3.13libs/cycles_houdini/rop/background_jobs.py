# SPDX-License-Identifier: Apache-2.0
"""Process ownership and paged reports without HOM, Qt or renderer access.

One daemon reaps owned children. It never terminates them on parent shutdown.
Only active processes and a bounded cache of handle-free history stay resident.
"""

from __future__ import annotations


from collections import OrderedDict
import ctypes
from dataclasses import dataclass
import heapq
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

TERMINAL = frozenset(("completed", "cancelled", "failed"))
_STATES = TERMINAL | {"preparing", "starting", "loading", "rendering", "stopping"}
# Protect only short Popen ownership operations, never I/O or process waits.
# One shared guard also avoids retaining a Windows lock handle per finished
# Job when a caller keeps its metadata after the history cache evicts it.
_process_lock = threading.RLock()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    try:
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.005)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, max_bytes=None):
    for attempt in range(20):
        try:
            with Path(path).open("rb") as stream:
                data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
            if max_bytes is not None and len(data) > max_bytes:
                raise ValueError("Report exceeds the preview size limit; inspect the saved file")
            return json.loads(data.decode("utf-8"))
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.005)


def process_identity(pid):
    """Read-only liveness plus Windows creation time; never adopt/signal a PID."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return dict(alive=False) if ctypes.get_last_error() == 87 else None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(
            handle, *[ctypes.byref(value) for value in times]
        ) or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return dict(created=(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime, alive=code.value == 259)
    finally:
        kernel.CloseHandle(handle)


def _key(directory):
    return os.path.normcase(str(Path(directory).resolve()))


def _marker(path):
    # Preserve the first request time when Stop is clicked repeatedly.
    try:
        with path.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        if not path.is_file():
            raise ValueError("Request path is not a file: " + str(path))


class Job:
    def __init__(self, directory, process=None):
        self.directory = Path(directory).resolve()
        self.key = _key(self.directory)
        self._process = process
        self._owned = process is not None
        self._pid = process.pid if process is not None else None
        self._returncode = None
        self._lock = _process_lock

    @property
    def id(self):
        return self.directory.name

    @property
    def owned(self):
        return self._owned

    @property
    def pid(self):
        if self._pid is None and (self.directory / "process.json").is_file():
            self._pid = self._saved_process()["pid"]
        return self._pid

    @property
    def has_process_handle(self):
        self.poll()
        with self._lock:
            return self._process is not None

    def poll(self):
        """Exact exit code for an owned child; None while running or unowned."""
        with self._lock:
            if self._process is not None:
                code = self._process.poll()
                if code is not None:
                    self._returncode = code
                    # Dropping Popen releases its OS handle. PID/exit metadata
                    # remain available, including to callers holding this Job.
                    self._process = None
            return self._returncode

    def wait(self, timeout=None):
        if not self.owned:
            raise RuntimeError("Waiting requires the original owned process handle")
        with self._lock:
            process = self._process
        if process is not None:
            process.wait(timeout=timeout)
        return self.poll()

    def is_running(self):
        return self.owned and self.poll() is None

    def _saved_process(self):
        value = read_json(self.directory / "process.json", 65536)
        if not isinstance(value, dict) or type(value.get("pid")) is not int or not 0 < value["pid"] < 2**32:
            raise ValueError("Invalid saved process PID")
        identity = value.get("identity")
        if identity is not None:
            if not isinstance(identity, dict) or type(identity.get("alive")) is not bool:
                raise ValueError("Invalid saved process identity")
            if "created" in identity and (type(identity["created"]) is not int or identity["created"] < 0):
                raise ValueError("Invalid saved process creation time")
        return value

    def _saved_status(self):
        value = read_json(self.directory / "status.json", 4 * 1024 * 1024)
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("id") != self.id
            or not isinstance(value.get("state"), str)
            or value["state"] not in _STATES
        ):
            raise ValueError("Invalid job status identity, schema or state")
        if not isinstance(value.get("rop"), str):
            raise ValueError("Invalid job ROP")
        for name in ("completed_frames", "skipped_frames"):
            if type(value.get(name)) is not int or value[name] < 0:
                raise ValueError("Invalid frame count: " + name)
        created = value.get("created")
        if type(created) not in (int, float) or not math.isfinite(created) or not 0 <= created <= 253402300799:
            raise ValueError("Invalid job creation time")
        for name in ("scene", "message", "error"):
            if name in value and not isinstance(value[name], str):
                raise ValueError("Invalid job text: " + name)
        if "frame" in value and (type(value["frame"]) not in (int, float) or not math.isfinite(value["frame"])):
            raise ValueError("Invalid job frame")
        return value

    def status(self):
        try:
            value = self._saved_status()
        except (OSError, ValueError, KeyError, UnicodeError, OverflowError) as error:
            # A damaged history entry must not prevent inspecting/stopping the
            # other jobs. Keep the damaged file intact for diagnosis.
            value = dict(
                schema=1,
                id=self.id,
                state="unavailable",
                rop=self.id,
                created=0,
                completed_frames=0,
                skipped_frames=0,
                error="Cannot read job report: " + str(error),
                report_error=str(error),
            )
        code = self.poll()
        exited = self.owned and code is not None
        if not self.owned and value["state"] not in TERMINAL and (self.directory / "process.json").is_file():
            try:
                process = self._saved_process()
                expected = process.get("identity")
                current = process_identity(process["pid"])
                exited = bool(
                    expected
                    and current
                    and (
                        not current["alive"]
                        or ("created" in expected and current.get("created") != expected["created"])
                    )
                )
            except (OSError, ValueError, KeyError, UnicodeError, OverflowError) as error:
                value["process_report_error"] = str(error)
        if exited and value["state"] not in TERMINAL:
            # The child may have published completion between our first read
            # and the exit check. Its final report takes precedence over the
            # older snapshot; never turn that successful exit into a failure.
            try:
                value = self._saved_status()
            except (OSError, ValueError, KeyError, UnicodeError, OverflowError) as error:
                value["report_error"] = str(error)
        if exited and value["state"] not in TERMINAL:
            forced = (self.directory / "force-stop").is_file()
            value.update(
                state="cancelled" if forced else "failed",
                forced=forced,
                error="Background render forcibly stopped"
                if forced
                else "Background Houdini exited before publishing completion; see render.log",
                finished=time.time(),
            )
            if code is not None:
                value["exit_code"] = code
            if not value.get("report_error"):
                write_json(self.directory / "status.json", value)
        stop = self.directory / "stop"
        if stop.is_file():
            value["stop_requested_wall_ns"] = stop.stat().st_mtime_ns
            if value["state"] not in TERMINAL:
                value.update(
                    worker_state=value["state"], state="stopping", message="Stop requested; waiting for the renderer"
                )
        return value

    def stop(self):
        if self.status()["state"] not in TERMINAL:
            _marker(self.directory / "stop")

    def terminate(self):
        """Low-level owned-process termination; an unexpected exit stays failed."""
        if not self.owned:
            raise RuntimeError("Forced termination requires the original process handle")
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()

    def force_stop(self):
        """Explicit user Stop intent, distinct from a crash or unexpected kill."""
        if not self.owned:
            raise RuntimeError("Force Stop requires the original process handle")
        if self.status()["state"] in TERMINAL:
            return
        self.stop()
        _marker(self.directory / "force-stop")
        self.terminate()


@dataclass(frozen=True)
class JobPage:
    jobs: tuple
    older_than: str | None = None


class JobRegistry:
    def __init__(self, history_limit=32):
        self.history_limit = history_limit
        self._active = {}
        self._recent = OrderedDict()
        self._condition = threading.Condition(threading.RLock())
        self._watcher = None
        self._shutdown = False

    def _remember(self, job):
        self._recent.pop(job.key, None)
        self._recent[job.key] = job
        while len(self._recent) > self.history_limit:
            self._recent.popitem(last=False)

    def adopt(self, directory, process):
        job = Job(directory, process)
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Background job registry is shutting down")
            if job.key in self._active:
                raise ValueError("Job already has an owned live process")
            self._recent.pop(job.key, None)
            self._active[job.key] = job
            if self._watcher is None or not self._watcher.is_alive():
                self._watcher = threading.Thread(target=self._watch, name="Cycles background jobs", daemon=True)
                self._watcher.start()
            self._condition.notify_all()
        return job

    def _watch(self):
        # No HOM, Qt, image assembly, metadata I/O or GPU operations here.
        while True:
            with self._condition:
                while not self._active and not self._shutdown:
                    self._condition.wait()
                if self._shutdown:
                    return
                active = tuple(self._active.items())
            for key, job in active:
                if job.poll() is not None:
                    with self._condition:
                        if self._active.get(key) is job:
                            self._active.pop(key)
                            self._remember(job)
            # Do not retain completed Job/Popen references in an idle watcher.
            active = ()
            job = None
            with self._condition:
                if not self._shutdown:
                    self._condition.wait(0.1)

    def shutdown(self):
        """Stop only the watcher; submitted jobs intentionally outlive Houdini."""
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
            watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=0.25)

    def active_jobs(self):
        with self._condition:
            return tuple(self._active.values())

    def get(self, directory):
        key = _key(directory)
        with self._condition:
            if key in self._active:
                return self._active[key]
            job = self._recent.get(key) or Job(directory)
            self._remember(job)
            return job

    def page(self, root, limit=50, before=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Choose a page size from 1 to 200")
        root = Path(root).resolve()
        # Reaping is intentionally asynchronous. Do not expose a finished job
        # from another project as active during the watcher's short grace period.
        with self._condition:
            all_active = tuple(job for job in self._active.values() if job.poll() is None)
        active = all_active if before is None else ()
        active_keys = {job.key for job in all_active}
        candidates = (
            (
                path
                for path in root.iterdir()
                if path.is_dir()
                and (path / "status.json").is_file()
                and (before is None or path.name < before)
                and os.path.normcase(str(path)) not in active_keys
            )
            if root.is_dir()
            else ()
        )
        paths = heapq.nlargest(limit + 1, candidates, key=lambda path: path.name)
        rows = active + tuple(self.get(path) for path in paths[:limit])
        rows = tuple(sorted(rows, key=lambda job: job.id, reverse=True))
        return JobPage(rows, paths[limit - 1].name if len(paths) > limit else None)

    def counts(self):
        with self._condition:
            return dict(
                active=len(self._active),
                cached_history=len(self._recent),
                history_limit=self.history_limit,
                watcher_alive=self._watcher is not None and self._watcher.is_alive(),
            )
