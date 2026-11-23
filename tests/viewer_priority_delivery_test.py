# SPDX-License-Identifier: Apache-2.0
"""Priority upgrades are bounded, current and use latest converted pixels."""

from pathlib import Path
import runpy
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

source = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini"
DisplayQueue = runpy.run_path(str(source / "ipr/display_queue.py"))["DisplayQueue"]
main_thread = threading.get_ident()


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class Object:
    def __init__(self, parent=None):
        self.destroyed = Signal()
        self.valid = True

    def event(self, event):
        return False


class Event:
    Type = int

    @staticmethod
    def registerEventType():
        return 1200

    def __init__(self, kind):
        self.kind = kind

    def type(self):
        return self.kind


class App(Object):
    def __init__(self):
        super().__init__()
        self.aboutToQuit = Signal()
        self.posted = []

    def thread(self):
        return main_thread

    def deliver(self):
        assert threading.get_ident() == main_thread
        index = max(range(len(self.posted)), key=lambda i: self.posted[i][2])
        receiver, event, priority = self.posted.pop(index)
        receiver.event(event)

    def drain(self):
        while self.posted:
            self.deliver()


app = App()
qt = SimpleNamespace(
    QObject=Object,
    QEvent=Event,
    Slot=lambda *args: lambda function: function,
    QCoreApplication=SimpleNamespace(
        instance=lambda: app, postEvent=lambda receiver, event, priority: app.posted.append((receiver, event, priority))
    ),
    QThread=SimpleNamespace(currentThread=threading.get_ident),
    Qt=SimpleNamespace(
        EventPriority=SimpleNamespace(
            HighEventPriority=SimpleNamespace(value=1), NormalEventPriority=SimpleNamespace(value=0)
        )
    ),
)
pyside = ModuleType("PySide6")
pyside.QtCore = qt
shiboken = ModuleType("shiboken6")
shiboken.isValid = lambda obj: obj.valid
old = {name: sys.modules.get(name) for name in ("PySide6", "shiboken6")}
sys.modules.update(PySide6=pyside, shiboken6=shiboken)
try:
    ViewerWakeup = runpy.run_path(str(source / "viewer/viewer_wakeup.py"))["ViewerWakeup"]
finally:
    for name, module in old.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class Receiver(Object):
    def __init__(self):
        super().__init__()
        self.calls = []

    def receive(self):
        self.calls.append(threading.get_ident())


r = Receiver()
current = ["first"]
wake = ViewerWakeup(r.receive, lambda token: token == current[0])
for _ in range(50000):
    wake.notify()
assert len(app.posted) == 1 and app.posted[0][2] == 0
wake.notify_first("first")
for _ in range(50000):
    wake.notify_first("first")
assert len(app.posted) == 2 and max(e[2] for e in app.posted) == 1
app.deliver()
assert r.calls == [main_thread], "First conversion did not upgrade stale normal notification"
app.drain()
assert r.calls == [main_thread], "Superseded normal pulse delivered twice"
# A new token upgrades a still-pending urgent event; only the new one delivers.
wake.notify_first("first")
current[0] = "second"
wake.notify_first("second")
app.drain()
assert len(r.calls) == 2
wake.notify_first("second")
current[0] = "third"
app.drain()
assert len(r.calls) == 2
# A later normal source change is not lost behind a stale priority pulse.
wake.notify_first("second")
wake.notify()
app.drain()
assert len(r.calls) == 3
wake.notify()
wake.close()
app.drain()
assert len(r.calls) == 3
wake.notify_first("third")
assert not app.posted
r2 = Receiver()
wake2 = ViewerWakeup(r2.receive)
wake2.notify_first("live")
r2.valid = False
app.drain()
assert not r2.calls
wake2.notify()
r2.destroyed.emit()
app.drain()
assert not r2.calls

# Changing-token flood while the GUI is busy must not allocate obsolete events.
r3 = Receiver()
latest = [None]
w3 = ViewerWakeup(r3.receive, lambda token: token == latest[0])
w3.notify()
normal_event = app.posted[0][1]
for index in range(20000):
    latest[0] = ("busy", index)
    w3.notify_first(latest[0])
    if index % 2:
        w3.notify()
assert len(app.posted) == 2 and app.posted[0][1] is normal_event
high_event = next(event for _, event, priority in app.posted if priority == 1)
assert sum(priority == 1 for _, _, priority in app.posted) == 1
app.deliver()
assert len(r3.calls) == 1
assert len(app.posted) == 1 and app.posted[0][1] is normal_event
# Priority consumed the logical normal request, but its physical event remains.
# A late normal reuses it; a new high reuses the same two physical slots.
for index in range(20000):
    w3.notify()
assert len(app.posted) == 1 and app.posted[0][1] is normal_event
app.deliver()
assert len(r3.calls) == 2
w3.notify()
late_normal = app.posted[0][1]
w3.notify_first(("obsolete",))
app.deliver()
assert len(r3.calls) == 2
w3.notify()
assert len(app.posted) == 1 and app.posted[0][1] is late_normal
app.drain()
assert len(r3.calls) == 3
# An outstanding normal may be delivered before high in a simulated race.
w3.notify()
w3.notify_first(latest[0])
receiver, event, _ = app.posted.pop(0)
receiver.event(event)
assert len(r3.calls) == 3
app.drain()
assert len(r3.calls) == 4
# Busy producer changes thousands of tokens while the main thread consumes.
maximum = [0]
errors = []
finished = threading.Event()


def post_checked(receiver, event, priority):
    app.posted.append((receiver, event, priority))
    maximum[0] = max(maximum[0], len(app.posted))
    if len(app.posted) > 2:
        errors.append("Physical queue exceeded two events")


qt.QCoreApplication.postEvent = post_checked


def producer():
    try:
        for index in range(20000):
            latest[0] = ("concurrent", index)
            w3.notify_first(latest[0])
            w3.notify()
            if index % 31 == 0:
                time.sleep(0)
    finally:
        finished.set()


thread = threading.Thread(target=producer)
thread.start()
while not finished.is_set() or app.posted:
    if app.posted:
        app.deliver()
    else:
        time.sleep(0)
thread.join(3)
assert not thread.is_alive() and not errors and maximum[0] <= 2
assert all(t == main_thread for t in r3.calls)
# A live producer can outlast receiver closure; retained events remain inert.
w3.notify()
w3.notify_first(latest[0])
before = len(r3.calls)
r3.destroyed.emit()
for index in range(20000):
    w3.notify_first(("closed", index))
    w3.notify()
assert len(app.posted) <= 2
app.drain()
assert len(r3.calls) == before and not w3.pending
print(
    "Passed physical bound: normal+high slots reused across20,000changing tokens, late normals, active producer and close"
)

# A finished/latest frame wins even if an earlier conversion posted priority.
converted = []
notices = []


def convert(frame, settings):
    assert threading.get_ident() != main_thread
    converted.append(frame)
    if frame == "error":
        raise ValueError("expected conversion failure")
    return dict(rgba=bytes([frame]))


def settled(queue, sequence):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with queue.condition:
            if queue.ready and queue.ready[1]["sequence"] == sequence:
                return
        time.sleep(0.001)
    raise TimeoutError("Conversion did not complete")


queue = DisplayQueue(
    convert, on_ready=lambda: notices.append("normal"), on_first_ready=lambda token: notices.append(token)
)
try:
    error_token = ("error",)
    queue.request(error_token, "error", 0, 0, {})
    deadline = time.monotonic() + 3
    while queue.ready is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert queue.take()[2] == "expected conversion failure" and notices == ["normal"]
    notices.clear()
    token = ("epoch", "settings", 1, 1)
    queue.request(token, 1, 1, 1, {})
    settled(queue, 1)
    queue.request(token, 2, 1, 2, {})
    settled(queue, 2)
    assert queue.take()[1]["sequence"] == 2 and notices == [token, "normal"]
    newer = ("epoch", "settings", 2, 2)
    queue.request(newer, 3, 2, 3, {})
    settled(queue, 3)
    assert notices[-1] == newer and queue.take()[1]["sequence"] == 3
    # Hidden/final-only consumers have no acknowledgement dependency or backlog.
    queue.request(newer, 4, 2, 4, {})
    settled(queue, 4)
    assert queue.take()[1]["sequence"] == 4 and notices[-1] == "normal"
finally:
    queue.close(wait=True)
assert queue.pending is queue.ready is None
print("Passed: priority upgrade, stale tokens/notifications, closed views, latest pixels and off-GUI conversion")
