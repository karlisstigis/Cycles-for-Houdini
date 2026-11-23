# SPDX-License-Identifier: Apache-2.0
"""One reusable deadline monitor for an exclusively owned worker connection.

Each exchange has a detection window. Long operations may also pass a
ceiling: when the window expires while the worker is observably active
(CPU time or I/O advancing), the deadline extends by another window, up to
the ceiling. A worker that stops making progress is killed after one window.
"""

import threading
import time


class ExchangeWatchdog:
    def __init__(self, process, activity=None, *, on_expire=None):
        self.process = process
        self.activity = activity
        # Called (on the monitor thread) after a deadline killed the worker; it must not raise.
        self.on_expire = on_expire
        self.condition = threading.Condition()
        self.deadline = None
        self.waiting_deadline = None
        self.window = self.ceiling = self.sample = None
        self.extensions = 0
        self.closed = False
        self.thread = threading.Thread(target=self.run, name="Cycles worker deadline", daemon=True)
        self.thread.start()

    def arm(self, timeout=60.0, *, ceiling=None):
        with self.condition:
            if self.closed:
                raise RuntimeError("Worker deadline monitor is closed")
            now = time.monotonic()
            self.window = timeout
            self.deadline = now + timeout
            self.ceiling = None if ceiling is None or self.activity is None else now + max(ceiling, timeout)
            self.sample = self.activity.sample() if self.ceiling is not None else None
            # Extending a deadline needs no wakeup. Under normal traffic the
            # monitor sleeps through thousands of completed exchanges.
            if self.waiting_deadline is None or self.deadline < self.waiting_deadline:
                self.condition.notify()

    def disarm(self):
        with self.condition:
            self.deadline = self.ceiling = self.sample = None

    def _extend(self, now):
        # Called under the condition. Only a long operation that is still
        # observably working earns another window.
        if self.ceiling is None or now >= self.ceiling:
            return False
        sample = self.activity.sample()
        progressed = self.activity.progressed(self.sample, sample)
        self.sample = sample
        if not progressed:
            return False
        self.deadline = min(now + self.window, self.ceiling)
        self.extensions += 1
        return True

    def run(self):
        with self.condition:
            while not self.closed:
                if self.deadline is None:
                    self.waiting_deadline = None
                    self.condition.wait()
                    continue
                now = time.monotonic()
                remaining = self.deadline - now
                if remaining > 0:
                    self.waiting_deadline = self.deadline
                    self.condition.wait(remaining)
                    continue
                if self._extend(now):
                    continue
                self.deadline = self.ceiling = self.sample = None
                # Serialize expiry with rearming/disarming. A deadline from an
                # older exchange must never kill a later, healthy exchange.
                try:
                    self.process.kill()
                except OSError:
                    pass  # An independently exited worker is already stopped.
                if self.on_expire is not None:
                    try:
                        self.on_expire()
                    except Exception:
                        pass

    def close(self):
        with self.condition:
            self.closed = True
            self.deadline = self.ceiling = self.sample = None
            self.condition.notify()
        if threading.current_thread() is not self.thread:
            self.thread.join()
