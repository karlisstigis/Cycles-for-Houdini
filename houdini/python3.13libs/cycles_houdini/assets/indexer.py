# SPDX-License-Identifier: Apache-2.0
"""Background scan job. The panel polls it from a host-thread timer.

The job thread only runs `provider.scan`; it never touches `hou` or Qt.
"""

import threading


class IndexJob:
    def __init__(self, provider, **scan_options):
        self.provider = provider
        self._options = scan_options
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = "idle"
        self._progress = (0, 0)
        self._result = None
        self._error = ""

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self._state = "running"
            self._thread = threading.Thread(target=self._run, name="CH asset library scan", daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def _report(self, done, total):
        with self._lock:
            self._progress = (int(done), int(total))

    def _run(self):
        try:
            result = self.provider.scan(cancelled=self._cancel.is_set, progress=self._report, **self._options)
        except Exception as error:
            with self._lock:
                self._state = "cancelled" if self._cancel.is_set() else "failed"
                self._error = str(error) or type(error).__name__
            return
        with self._lock:
            self._result = result
            self._state = "cancelled" if self._cancel.is_set() or getattr(result, "cancelled", False) else "done"

    def poll(self):
        """Non-blocking snapshot; None before start()."""
        with self._lock:
            if self._thread is None:
                return None
            return {"state": self._state, "progress": self._progress, "result": self._result, "error": self._error}

    def join(self, timeout=None):
        """For tests and shutdown; the panel should poll instead."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.poll()
