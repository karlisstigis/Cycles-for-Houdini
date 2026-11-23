# SPDX-License-Identifier: Apache-2.0
"""Thread-safe optional runtime loading, with retry after startup repair."""

from importlib import import_module
import threading


class LazyModule:
    """Resolve an optional host module only when an attribute is used."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        return getattr(import_module(self.name), name)


class NativeRuntime:
    def __init__(self, loader):
        self._loader = loader
        self._module = None
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            if self._module is None:
                # Commit only a fully validated and initialized extension. A
                # missing provider/environment can be repaired and retried.
                module = self._loader()
                self._module = module
            return self._module

    def __getattr__(self, name):
        return getattr(self.load(), name)
