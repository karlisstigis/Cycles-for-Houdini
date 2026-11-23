# SPDX-License-Identifier: Apache-2.0
"""Host-owned prototype leases for synchronous and queued renderer consumers.

Only the union of accepted, active and pending sample identities is pinned. A
finalizer queues an opaque token; cache/material operations happen when the host
next drains releases, never on a GC or conversion thread.
"""

import queue
import threading
import uuid
import weakref

_releases = queue.SimpleQueue()


def _assets():
    from . import blender_assets

    return blender_assets


def drain_releases():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Prototype lease retirement requires the host thread")
    while True:
        try:
            token = _releases.get_nowait()
        except queue.Empty:
            return
        _assets().release_consumer(token)


def split_update(update):
    """Keep the Python ownership ledger outside native/worker update schemas."""
    payload = dict(update)
    metadata = payload.pop("_blender_prototypes", None)
    return payload, metadata


def session_leases(session):
    leases = getattr(session, "_prototype_leases", None)
    if leases is None:
        leases = session._prototype_leases = PrototypeLeases(session)
    return leases


class PrototypeLeases:
    def __init__(self, session, assets=None):
        self.token = uuid.uuid4().hex
        self.accepted = set()
        self.requests = {}
        self._session = weakref.ref(session)
        self._assets_override = assets
        self._registered = False
        self._finalizer = None
        self._closed = False

    def _backend(self):
        return self._assets_override if self._assets_override is not None else _assets()

    def _sync(self, *, register=False):
        if self._registered or register:
            keys = set(self.accepted)
            for pending in self.requests.values():
                if pending is not None:
                    keys.update(pending)
            self._backend().retain_consumer(self.token, keys)
            self._registered = True
            if self._finalizer is None:
                session = self._session()
                if session is not None:
                    self._finalizer = weakref.finalize(session, _releases.put, self.token)

    def begin(self, submission, metadata):
        if self._closed:
            raise RuntimeError("Prototype consumer is closed")
        if metadata is None:
            self.requests[submission] = None
            return
        if (
            not isinstance(metadata, dict)
            or "rop" not in metadata
            or not isinstance(metadata.get("keys"), (list, tuple))
        ):
            raise ValueError("Malformed Blender prototype ownership metadata")
        keys = set(metadata["keys"])
        self.requests[submission] = keys
        # Establish the consumer's complete union before transferring the ROP pin.
        self._sync(register=True)
        self._backend().release_capture(metadata["rop"])

    def rename(self, previous, submission):
        self.requests[submission] = self.requests.pop(previous)

    def inherit(self, submission, previous):
        """A camera-only delta retains the older pending full-scene samples."""
        if self.requests.get(submission) is None and previous in self.requests:
            value = self.requests[previous]
            self.requests[submission] = None if value is None else set(value)
        self._sync()

    def commit(self, submission):
        if submission not in self.requests:
            return
        keys = self.requests.pop(submission)
        if keys is not None:
            self.accepted = keys
        self._sync()

    def drop(self, submission):
        if submission in self.requests:
            self.requests.pop(submission)
            self._sync()

    def clear(self, *, keep=()):
        self.accepted.clear()
        keep = set(keep)
        self.requests = {submission: keys for submission, keys in self.requests.items() if submission in keep}
        self._sync()

    def close(self):
        if self._registered:
            self._backend().release_consumer(self.token)
            self._registered = False
        if self._finalizer is not None:
            self._finalizer.detach()
        self.accepted.clear()
        self.requests.clear()
        self._closed = True
