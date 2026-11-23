# SPDX-License-Identifier: Apache-2.0
"""Drain a worker's stderr even if diagnostic storage is slow or unavailable."""

import queue
import re
import threading
import time

from .session_log import prune_logs

LIMIT_BYTES = 4 * 1024 * 1024
RETAIN = 64
_CHUNK_BYTES = 16 * 1024
_CHUNKS = 16
_NOTE_BYTES = 160
_NAME = re.compile(r"[0-9a-f]{32}\.log\Z")


class WorkerLog:
    def __init__(self, path, stderr):
        self.path, self.stderr = path, stderr
        self.chunks = queue.Queue(maxsize=_CHUNKS)
        self.drained = threading.Event()
        self.write_stopped = threading.Event()
        self.omitted = 0
        self.reader = threading.Thread(target=self._read, name="Cycles worker stderr reader", daemon=True)
        self.writer = threading.Thread(target=self._write, name="Cycles worker log writer", daemon=True)
        self.reader.start()
        self.writer.start()

    def _read(self):
        accepted = 0
        try:
            with self.stderr:
                while data := self.stderr.read(_CHUNK_BYTES):
                    kept = data[: max(0, LIMIT_BYTES - _NOTE_BYTES - accepted)]
                    if kept and not self.write_stopped.is_set():
                        try:
                            self.chunks.put_nowait(kept)
                            accepted += len(kept)
                        except queue.Full:
                            kept = b""
                    else:
                        kept = b""
                    self.omitted += len(data) - len(kept)
        except OSError:
            pass
        finally:
            self.drained.set()

    def _write(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("xb") as stream:
                prune_logs(self.path, lambda other: _NAME.fullmatch(other.name), RETAIN)
                while True:
                    try:
                        chunk = self.chunks.get(timeout=0.05)
                    except queue.Empty:
                        if self.drained.is_set() and self.chunks.empty():
                            break
                        continue
                    stream.write(chunk)
                    stream.flush()
                if self.omitted:
                    note = f"\nCycles worker log omitted {self.omitted} bytes (size limit or busy writer).\n".encode()
                    stream.write(note[: min(_NOTE_BYTES, max(0, LIMIT_BYTES - stream.tell()))])
        except (OSError, ValueError):
            pass
        finally:
            self.write_stopped.set()

    def finish(self, timeout=0.2):
        """Give the last error a chance to reach disk without holding up retirement."""
        deadline = time.monotonic() + timeout
        self.reader.join(timeout)
        self.writer.join(max(0, deadline - time.monotonic()))
