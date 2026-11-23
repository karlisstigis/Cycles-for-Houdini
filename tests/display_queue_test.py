# SPDX-License-Identifier: Apache-2.0
"""Bounded conversion and retirement during deliberately outstanding work."""

import runpy
import threading
import time
from pathlib import Path

DisplayQueue = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/ipr/display_queue.py")
)["DisplayQueue"]
started = threading.Event()
release = threading.Event()
calls = []


def convert(frame, settings):
    calls.append(frame)
    if frame == 1:
        started.set()
        assert release.wait(3)
    return dict(rgba=bytes([frame]))


def result(queue):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = queue.take()
        if value is not None:
            return value
        time.sleep(0.001)
    raise TimeoutError("Display conversion did not finish")


queue = DisplayQueue(convert)
try:
    queue.request(("old",), 1, 1, 1, {})
    assert started.wait(3)
    # These return while conversion is blocked. Only the newest pending input
    # survives, and an obsolete session/settings result cannot reach the UI.
    queue.request(("old",), 2, 1, 2, {})
    queue.invalidate(("new",))
    queue.request(("new",), 3, 2, 3, {})
    assert calls == [1] and queue.take() is None
    release.set()
    token, image, error = result(queue)
    assert token == ("new",) and image["rgba"] == bytes([3]) and error is None
    assert calls == [1, 3] and image["generation"] == 2 and image["sequence"] == 3
    queue.request(("new",), 3, 2, 3, {})
    assert queue.pending is None, "Same frame converted twice"
    queue.request(("new",), 4, 2, 4, {})
    _, image, error = result(queue)
    assert image["rgba"] == bytes([4]) and error is None
finally:
    release.set()
    queue.close(wait=True)
assert not queue.thread.is_alive() and queue.pending is None and queue.ready is None


class OwnedConverter:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(("convert", threading.get_ident()))
        return dict(rgba=b"owned")

    def close(self):
        self.calls.append(("close", threading.get_ident()))


owned = OwnedConverter()
queue = DisplayQueue(owned)
queue.request(("owned",), 1, 1, 1, {})
result(queue)
queue.close(wait=True)
queue.close(wait=True)
assert len(owned.calls) == 2 and owned.calls[0][1] == owned.calls[1][1] != threading.get_ident()
assert queue.cleanup_error is None
unused = OwnedConverter()
queue = DisplayQueue(unused)
queue.close()
queue.close()
assert unused.calls == [("close", threading.get_ident())]

# Completion notifications run outside the queue lock and never fire for a
# retired token or after close, even when a conversion finishes much later.
started.clear()
release.clear()
notifications = []
notified = threading.Event()


def wakeup():
    assert not queue.condition._is_owned()
    notifications.append(threading.get_ident())
    notified.set()


queue = DisplayQueue(convert, on_ready=wakeup)
try:
    queue.request(("discard",), 1, 1, 1, {})
    assert started.wait(3)
    queue.invalidate(("accepted",))
    queue.request(("accepted",), 5, 2, 5, {})
    release.set()
    assert notified.wait(3)
    assert result(queue)[1]["rgba"] == bytes([5])
    assert notifications == [queue.thread.ident]
finally:
    release.set()
    queue.close(wait=True)
started.clear()
release.clear()
notified.clear()
queue = DisplayQueue(convert, on_ready=wakeup)
queue.request(("closing",), 1, 1, 1, {})
assert started.wait(3)
queue.close()
release.set()
queue.close(wait=True)
assert not notified.is_set() and len(notifications) == 1


class Source:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = (0, 0, None)
        self.extra = False

    def state(self, after):
        with self.lock:
            return self.value

    def publish(self, revision, sequence):
        with self.lock:
            self.value = revision, revision, (sequence, revision, sequence)

    def describe(self, frame):
        passes = ["combined", "normal", "rgb"] + (["repaired"] if self.extra else [])
        return dict(
            passes=passes,
            pass_metadata={
                name: dict(
                    color=name != "normal", channels=["R", "G", "B", "A"] if name == "combined" else ["R", "G", "B"]
                )
                for name in passes
            },
        )


# No GUI tick is involved: a producer queues conversion while input is being
# replaced. Camera revisions and automatically changed display choices must
# retain distinct identities, even when the user epoch happens to be unchanged.
source = Source()
started.clear()
release.clear()
calls.clear()
options = []


def described_convert(frame, settings):
    options.append(dict(settings))
    return convert(frame, settings)


queue = DisplayQueue(described_convert)
try:
    queue.follow(source.state, 0, {}, source.describe)
    source.publish(1, 1)
    publisher = threading.Thread(target=queue.source_ready)
    publisher.start()
    publisher.join(3)
    assert started.wait(3)
    for frame in range(2, 101):
        source.publish(2, frame)
        queue.source_ready()
    assert calls == [1]
    release.set()
    token, image, error = result(queue)
    assert error is None and image["rgba"] == bytes([100]) and token[2:] == (2, 2)
    assert calls == [1, 100]
    queue.follow(source.state, 0, {"pass": "normal", "channels": "alpha"}, source.describe)
    changed, image, error = result(queue)
    assert changed != token and not queue.is_current(token) and error is None
    assert options[-1]["channels"] == "rgba", "Data pass inherited alpha-only processing"
    queue.follow(source.state, 0, {"pass": "rgb", "channels": "alpha"}, source.describe)
    result(queue)
    assert options[-1]["channels"] == "rgb", "RGB-only pass inherited unavailable alpha"
    count = len(calls)
    queue.follow(source.state, 0, {"pass": "repaired"}, source.describe)
    assert len(calls) == count and queue.take() is None, "Unavailable pass converted before repair"
    source.extra = True
    source.publish(3, 101)
    queue.source_ready()
    assert result(queue)[1]["rgba"] == bytes([101]) and options[-1]["pass"] == "repaired"
    queue.follow(None, 1, {}, None)
    assert queue.pending is None and queue.ready is None and queue.source_config is None
finally:
    release.set()
    queue.close(wait=True)

# A GUI configuration change or closure cannot wait on a producer reading an
# owned source. Its obsolete result is discarded after the read eventually ends.
reading = threading.Event()
read_release = threading.Event()


def held_source(after):
    reading.set()
    assert read_release.wait(3)
    return source.state(after)


queue = DisplayQueue(described_convert)
try:
    queue.follow(source.state, 2, {}, source.describe)
    result(queue)
    publisher = threading.Thread(target=lambda: queue.follow(held_source, 3, {}, source.describe))
    publisher.start()
    assert reading.wait(3)
    began = time.monotonic()
    queue.follow(source.state, 4, {}, source.describe)
    queue.close()
    assert time.monotonic() - began < 0.1, "GUI waited on a source read during configuration/close"
    read_release.set()
    publisher.join(3)
    assert not publisher.is_alive() and queue.take() is None
finally:
    read_release.set()
    queue.close(wait=True)
print("Display queue: bounded replacement, stale retirement, progressive delivery and shutdown passed")
