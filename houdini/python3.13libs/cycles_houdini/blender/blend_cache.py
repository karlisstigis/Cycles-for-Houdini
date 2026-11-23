# SPDX-License-Identifier: Apache-2.0
"""Byte budgets for Blender assets, shared buffers and in-flight transfers.

Targets describe owned cache storage, not process RSS or renderer/device memory.
Insertion is transactional: callers install entries, pin the full live set, then
trim. A live render may exceed its retention target and is never evicted for it.
"""

from collections import OrderedDict
from collections.abc import MutableMapping
from contextlib import contextmanager
import os
import threading


MiB = 1024 * 1024


def budget_bytes(environment, default_mib, *, minimum=MiB, maximum=64 * 1024 * MiB):
    """Read a positive MiB target; reject invalid settings instead of unbounding it."""
    value = os.environ.get(environment)
    try:
        amount = float(default_mib if value is None else value)
        result = int(amount * MiB)
    except (ValueError, OverflowError):
        raise ValueError(environment + " must be a finite MiB value") from None
    if not minimum <= result <= maximum:
        raise ValueError(f"{environment} must be between {minimum / MiB:g} and {maximum / MiB:g} MiB")
    return result


def buffer_storage(value):
    """Find numeric backing allocations once, including NumPy views over bytes.

    No NumPy import is needed in Blender's bootstrap. Metadata is deliberately
    excluded; native assets supply explicit capacity records instead.
    """
    found, visited = {}, set()

    def visit(item):
        if id(item) in visited:
            return
        visited.add(id(item))
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif hasattr(item, "nbytes"):
            root = item
            seen = set()
            while id(root) not in seen:
                seen.add(id(root))
                parent = root.obj if isinstance(root, memoryview) else getattr(root, "base", None)
                if parent is None:
                    break
                root = parent
            try:
                size = memoryview(root).nbytes
            except TypeError:
                size = int(getattr(root, "nbytes", item.nbytes))
            found[("buffer", id(root))] = size
        elif isinstance(item, (bytes, bytearray, memoryview)):
            root = item.obj if isinstance(item, memoryview) else item
            found[("buffer", id(root))] = memoryview(root).nbytes

    visit(value)
    return found


class ByteBudgetCache(MutableMapping):
    """LRU retention with owner pins and unique allocation accounting.

    ``storage(value)`` returns {stable allocation identity: allocated bytes};
    ``size(value)`` is convenient for values without shared storage. These
    callbacks are alternatives. Owners are live ROP IDs or transaction tokens.
    Values must keep immutable allocations while cached: accounting is captured
    once on insertion/replacement, never by revisiting native buffers on hits.
    """

    def __init__(
        self, target_bytes, *, size=None, storage=None, label="Blender assets", max_entries=None, describe=str
    ):
        if target_bytes < 0 or size is not None and storage is not None:
            raise ValueError("Invalid cache budget or accounting callbacks")
        self.target_bytes = int(target_bytes)
        self.label = label
        self.max_entries = max_entries
        self._describe = describe
        self._entries = OrderedDict()
        self._allocation_records = {}
        self._pins = {}
        self._summary = None
        if size is not None:
            self._storage = lambda value: {("value", id(value)): size(value)}
        else:
            self._storage = storage or buffer_storage

    def __getitem__(self, key):
        result = self._entries[key]
        self._entries.move_to_end(key)
        return result

    def __setitem__(self, key, value):
        record = dict(self._storage(value))
        if any(type(amount) is not int or amount < 0 for amount in record.values()):
            raise ValueError("Cache storage accounting requires nonnegative integer byte counts")
        self._entries[key] = value
        self._entries.move_to_end(key)
        self._allocation_records[key] = record
        self._summary = None

    def __delitem__(self, key):
        del self._entries[key]
        del self._allocation_records[key]
        self._summary = None

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def values(self):
        # Mapping's default values() invokes __getitem__, which changes LRU order.
        return self._entries.values()

    def items(self):
        return self._entries.items()

    def clear(self):
        self._entries.clear()
        self._allocation_records.clear()
        self._pins.clear()
        self._summary = None

    def pin(self, owner, keys):
        keys = set(keys)
        if self._pins.get(owner) != keys:
            self._pins[owner] = keys
            self._summary = None

    def unpin(self, owner):
        if owner in self._pins:
            del self._pins[owner]
            self._summary = None

    def _records(self):
        return self._allocation_records

    def snapshot(self, *, largest=5):
        if self._summary is not None and self._summary["target_bytes"] == self.target_bytes:
            return dict(
                self._summary, largest_assets=[dict(item) for item in self._summary["largest_assets"][:largest]]
            )
        records = self._records()
        active = set().union(*self._pins.values()) if self._pins else set()

        def unique(keys):
            allocations = {}
            for key in keys:
                for ident, amount in records[key].items():
                    allocations[ident] = max(amount, allocations.get(ident, 0))
            return sum(allocations.values())

        resident = unique(records)
        active_bytes = unique(records.keys() & active)
        biggest = sorted(records, key=lambda key: sum(records[key].values()), reverse=True)
        self._summary = dict(
            label=self.label,
            target_bytes=self.target_bytes,
            resident_bytes=resident,
            active_bytes=active_bytes,
            over_budget_bytes=max(0, resident - self.target_bytes),
            active_over_budget_bytes=max(0, active_bytes - self.target_bytes),
            entries=len(records),
            pinned_entries=len(records.keys() & active),
            largest_assets=[
                dict(asset=self._describe(key), bytes=sum(records[key].values()), active=key in active)
                for key in biggest
            ],
        )
        return dict(self._summary, largest_assets=[dict(item) for item in self._summary["largest_assets"][:largest]])

    def trim(self):
        state = self.snapshot()
        if not state["over_budget_bytes"] and (self.max_entries is None or len(self) <= self.max_entries):
            return dict(state, evicted=[])
        active = set().union(*self._pins.values()) if self._pins else set()
        records = self._records()
        references, allocations = {}, {}
        for record in records.values():
            for ident, amount in record.items():
                references[ident] = references.get(ident, 0) + 1
                allocations[ident] = max(amount, allocations.get(ident, 0))
        resident = sum(allocations.values())
        evicted = []
        for key in tuple(self._entries):
            if resident <= self.target_bytes and (self.max_entries is None or len(self) <= self.max_entries):
                break
            if key not in active:
                record = records[key]
                del self[key]
                evicted.append(key)
                for ident in record:
                    references[ident] -= 1
                    if not references[ident]:
                        resident -= allocations[ident]
        return dict(self.snapshot(), evicted=evicted)


class InFlightBudget:
    """A strict aggregate reservation bound, held through conversion/consumption.

    Reservations fail before allocation when an asset or concurrent demand cannot
    fit. The caller can serialize/retry; there is no waiting while holding RPC or
    worker locks, which could deadlock another reservation's release.
    """

    def __init__(self, target_bytes):
        if type(target_bytes) is not int or target_bytes < 0:
            raise ValueError("Invalid in-flight Blender byte target")
        self.target_bytes = int(target_bytes)
        self.used_bytes = 0
        self.peak_bytes = 0
        self._lock = threading.Lock()

    def reserve(self, nbytes):
        if type(nbytes) is not int or nbytes < 0:
            raise ValueError("Invalid in-flight Blender byte count")
        with self._lock:
            if self.used_bytes + nbytes > self.target_bytes:
                raise MemoryError(
                    f"Blender in-flight geometry requires {self.used_bytes + nbytes} bytes; "
                    f"target is {self.target_bytes} bytes"
                )
            self.used_bytes += nbytes
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)

    def release(self, nbytes):
        with self._lock:
            if type(nbytes) is not int or not 0 <= nbytes <= self.used_bytes:
                raise ValueError("Invalid in-flight Blender release")
            self.used_bytes -= nbytes

    @contextmanager
    def lease(self, nbytes):
        self.reserve(nbytes)
        try:
            yield
        finally:
            self.release(nbytes)
