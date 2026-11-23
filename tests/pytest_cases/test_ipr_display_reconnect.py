# SPDX-License-Identifier: Apache-2.0
"""A broken image socket must not cancel the registry-owned IPR session."""

import sys
from types import ModuleType, SimpleNamespace
import pytest

from support.package_loader import load_module, prepare_package

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_prerender_allows_native_ipr_to_submit_before_any_local_final(monkeypatch):
    package = ModuleType("test_native_prerender_fixture")
    hou = SimpleNamespace(
        isUIAvailable=lambda: True,
        timeToFrame=lambda t: t * 24 + 1,
        ropRenderEventType=SimpleNamespace(PreRender=1, PostRender=2),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    prepare_package(package)
    package.ipr.registry = SimpleNamespace()  # Acquiring a suspension here would deadlock IPR's first image.
    package.scene.native_scene = SimpleNamespace()
    package.rop.output = SimpleNamespace()
    try:
        final = load_module(package, "final_render")
        rop = SimpleNamespace(sessionId=lambda: 7)
        final._render_event(rop, hou.ropRenderEventType.PreRender, 0)
        assert final._active[7].pause_token is None
    finally:
        for name in tuple(sys.modules):
            if name == package.__name__ or name.startswith(package.__name__ + "."):
                sys.modules.pop(name, None)


class DisplayConnectionError(RuntimeError):
    pass


class DisplayRemoteQuit(RuntimeError):
    pass


class Session:
    def __init__(self):
        self.frame = object()
        self.generation = 7
        self.presented = []
        self.opens = []
        self.closes = 0
        self.cancels = 0
        self.failure = None

    def present_native(self):
        self.presented.append(self.frame)
        if self.failure:
            raise self.failure

    def open_native_display(self, destination, port):
        self.opens.append((destination, port))

    def close_native_display(self):
        self.closes += 1

    def cancel(self):
        self.cancels += 1

    def status(self):
        return {"state": "rendering", "generation": self.generation}


class Bridge:
    def __init__(self):
        self.events = []
        self.unbound = []

    def drain(self):
        result, self.events = self.events, []
        return result

    def unbind(self, channel):
        self.unbound.append(channel)


class Clock:
    value = 100.0

    def monotonic(self):
        return self.value


def test_native_display_reopen_keeps_preview_selection(monkeypatch):
    import cycles_houdini

    opens, previews = [], []
    monkeypatch.setattr(
        cycles_houdini,
        "_native",
        SimpleNamespace(
            open_display=lambda handle, destination, port, options: opens.append((handle, destination, port, options))
        ),
    )
    session = cycles_houdini.Session.__new__(cycles_houdini.Session)
    session._handle = object()
    session._native_preview = False
    session._set_preview_pass = previews.append
    session.open_native_display("ip", "43210")
    session.open_native_display("ip", "43210")
    assert len(opens) == 2
    assert all(row[1:] == ("ip", "43210", {}) for row in opens)
    assert previews == ["*"]


def test_registry_reconnect_preserves_render_and_stop_during_backoff(monkeypatch):
    package_name = "test_ipr_reconnect_fixture"
    package = ModuleType(package_name)
    package.Session = Session
    previous_color = type("PreviousColorConfiguration", (RuntimeError,), {})
    package._native = SimpleNamespace(
        DisplayConnectionError=DisplayConnectionError, PreviousColorConfiguration=previous_color
    )
    package.synchronize_color_configuration = lambda: None
    rop = SimpleNamespace(path=lambda: "/out/cycles")
    viewer = SimpleNamespace(type=lambda: "ipr", ropNode=lambda: rop)
    hou = SimpleNamespace(
        isUIAvailable=lambda: False,
        ObjectWasDeleted=type("ObjectWasDeleted", (Exception,), {}),
        paneTabType=SimpleNamespace(IPRViewer="ipr"),
        ui=SimpleNamespace(paneTabs=lambda: [viewer]),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    prepare_package(package)
    package.scene.native_scene = SimpleNamespace()
    registry = load_module(package, "registry")
    clock = Clock()
    bridge = Bridge()
    session = Session()
    entry = registry.Entry("rop:1", ("CPU", 16, 16, 4), session, rop=rop, views=1)
    entry.channel, entry.socket_port, entry.helper_pid = "control", "43210", 1234
    registry._entries[entry.key] = entry
    registry._bridge = bridge
    monkeypatch.setattr(registry, "time", clock)
    monkeypatch.setattr(registry, "_poll_local_finals", lambda: None)

    def poll(at):
        clock.value = at
        registry._last_texture_poll = at
        registry._last_display = 0.0
        registry._poll_host()

    try:
        # The exact controller, retained frame and bridge binding survive.
        session.failure = DisplayConnectionError("socket lost")
        poll(100.0)
        assert entry.session is session and entry.display_retry_at == 100.1
        assert session.cancels == session.closes == 0
        assert (entry.channel, entry.socket_port) == ("control", "43210")
        poll(100.05)
        assert len(session.presented) == 1 and not session.opens

        session.failure = None
        poll(100.1)
        assert session.opens == [("ip", "43210")]
        assert session.presented == [session.frame, session.frame]
        assert session.generation == 7 and entry.display_retry_at == 0.0

        # Repeated failures back off to a two-second maximum, then a healthy
        # connection clears the failure count without restarting the render.
        session.failure = DisplayConnectionError("socket lost again")
        poll(100.2)
        assert entry.display_retry_failures == 2
        for index in range(3, 9):
            poll(entry.display_retry_at)
            assert entry.display_retry_failures == min(index, 6)
            assert round(entry.display_retry_at - clock.value, 3) <= 2.0
        session.failure = None
        poll(entry.display_retry_at)
        poll(clock.value + 1.0)
        assert entry.display_retry_failures == 0 and session.cancels == 0

        session.failure = previous_color("Previous color configuration")
        poll(clock.value + 0.1)
        assert entry.previous_color_notice and session.cancels == session.closes == 0
        assert entry.display_retry_at == 0 and entry.socket_port
        session.failure = None
        poll(clock.value + 0.1)
        assert not entry.previous_color_notice

        # Stop while a retry is pending must cancel immediately and never
        # attempt the due reopen, even when the image peer remains absent.
        session.failure = DisplayConnectionError("socket lost before Stop")
        poll(clock.value + 0.1)
        opens_before_stop = list(session.opens)
        bridge.events = [(entry.key, entry.channel, "closed", None)]
        poll(clock.value + 0.01)
        assert session.cancels == session.closes == 1
        assert entry.socket_port == entry.channel == ""
        poll(clock.value + 5.0)
        assert session.opens == opens_before_stop

        # Explicit MPlay remote quit still follows the fatal Stop path.
        entry.socket_port, entry.channel = "43210", "next-control"
        session.failure = DisplayRemoteQuit("viewer stopped")
        poll(clock.value + 0.1)
        assert session.cancels == session.closes == 2
        assert bridge.unbound == ["next-control"]
    finally:
        registry._entries.clear()
        registry._bridge = None
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)
