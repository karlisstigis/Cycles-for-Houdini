# SPDX-License-Identifier: Apache-2.0
"""Exercise real panel scheduling methods without loading Houdini or Qt.

Only the host/UI surfaces are faked. Methods come from the owned RenderPanel
class, so queue coalescing, manual priority and source debounce run unchanged.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.fixture
def panel():
    source = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer/panel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RenderPanel")
    methods = {
        "_observe_timeline",
        "_capture_timeline_complete",
        "_queue_automatic_update",
        "request_update",
        "queue_render",
        "_run_queued_render",
        "cancel",
        "ui_active",
    }
    extracted = ast.ClassDef(
        name="Panel",
        bases=[],
        keywords=[],
        body=[node for node in definition.body if isinstance(node, ast.FunctionDef) and node.name in methods],
        decorator_list=[],
    )
    clock = SimpleNamespace(now=10.0, monotonic=lambda: clock.now)
    host = SimpleNamespace(now=1.0, isUIAvailable=lambda: True)
    callbacks = []
    host.ui = SimpleNamespace(postEventCallback=callbacks.append)
    namespace = dict(time=clock, hou=host)
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[extracted], type_ignores=[])), str(source), "exec"),
        namespace,
    )
    instance = namespace["Panel"]()
    instance._disposed = instance._in_render = instance._render_queued = instance._force_render_pending = False
    instance._stopped = instance._pending = False
    instance._final_view = None
    instance._last_time = 1.0
    instance._timeline_pending_time = None
    instance._changed_at = instance._pending_since = 0.0
    instance._update_serial = 0
    instance.auto = SimpleNamespace(checked=True, isChecked=lambda: instance.auto.checked)
    instance.entry = SimpleNamespace(
        channel="",
        paused=False,
        final_paused=False,
        camera_only_pending=False,
        light_values_pending=False,
        # A real registry entry holds the Light Linker receivers still to apply.
        linking_values_pending=set(),
        material_values_pending=False,
        defer_source_capture=lambda: False,
        stop_serial=0,
        final_update_pending=False,
        session=SimpleNamespace(cancel=lambda: None),
    )
    instance._camera_capture_allowed = lambda: False
    instance.sync_preview_controls = instance.update_render_controls = lambda: None
    captures = []

    def render(*, reuse_if_unchanged):
        captured = host.now
        serial = instance._update_serial
        captures.append((captured, reuse_if_unchanged))
        instance._capture_timeline_complete(captured)
        instance._pending = serial != instance._update_serial

    instance.render = render
    return SimpleNamespace(panel=instance, clock=clock, host=host, callbacks=callbacks, captures=captures)


def tick(state):
    state.panel._observe_timeline(state.host.now)
    state.panel._queue_automatic_update(state.clock.now)


def dispatch(state):
    callback = state.callbacks.pop(0)
    callback()


def test_timeline_bypasses_only_parameter_debounce(panel):
    state = panel
    state.panel.request_update()
    state.panel._queue_automatic_update(state.clock.now)
    assert not state.callbacks
    state.clock.now += 0.079
    state.panel._queue_automatic_update(state.clock.now)
    assert not state.callbacks
    state.host.now = 2
    tick(state)
    assert len(state.callbacks) == 1 and not state.captures
    dispatch(state)
    assert state.captures == [(2, True)]


def test_parameter_only_capture_keeps_80ms_debounce(panel):
    panel.panel.request_update()
    panel.clock.now += 0.081
    panel.panel._queue_automatic_update(panel.clock.now)
    assert len(panel.callbacks) == 1


def test_rapid_timeline_changes_coalesce_to_latest_frame(panel):
    for frame in (2, 3, 4):
        panel.host.now = frame
        tick(panel)
    assert len(panel.callbacks) == 1
    dispatch(panel)
    assert panel.captures == [(4, True)]
    for _ in range(10):
        tick(panel)
    assert not panel.callbacks and len(panel.captures) == 1


def test_frame_arriving_during_capture_keeps_source_request(panel):
    panel.host.now = 2
    tick(panel)
    panel.panel._in_render = True
    panel.host.now = 3
    tick(panel)
    assert len(panel.callbacks) == 1
    panel.panel._capture_timeline_complete(2)
    assert panel.panel._timeline_pending_time == 3
    panel.panel._in_render = False
    dispatch(panel)
    assert panel.captures == [(3, True)]
    assert panel.panel._timeline_pending_time is None


def test_new_time_not_observed_during_capture_is_seen_next_poll(panel):
    panel.host.now = 2
    tick(panel)
    panel.host.now = 3
    panel.panel._capture_timeline_complete(2)
    tick(panel)
    dispatch(panel)
    assert panel.captures == [(3, True)]


@pytest.mark.parametrize("gate", ["auto", "stop", "pause", "final_pause", "native_channel", "defer"])
def test_timeline_retains_all_automatic_capture_gates(panel, gate):
    if gate == "auto":
        panel.panel.auto.checked = False
    elif gate == "stop":
        panel.panel._stopped = True
    elif gate == "pause":
        panel.panel.entry.paused = True
    elif gate == "final_pause":
        panel.panel.entry.final_paused = True
    elif gate == "native_channel":
        panel.panel.entry.channel = "owned native display"
    elif gate == "defer":
        panel.panel.entry.defer_source_capture = lambda: True
    panel.host.now = 2
    tick(panel)
    assert not panel.callbacks and panel.panel._pending
    assert panel.panel._timeline_pending_time == 2


def test_paused_frame_is_captured_once_when_resumed(panel):
    panel.panel.entry.paused = True
    panel.host.now = 2
    tick(panel)
    panel.host.now = 3
    tick(panel)
    assert not panel.callbacks
    panel.panel.entry.paused = False
    tick(panel)
    dispatch(panel)
    assert panel.captures == [(3, True)]


def test_manual_refresh_priority_survives_timeline_coalescing(panel):
    panel.panel.queue_render(reuse_if_unchanged=False)
    panel.host.now = 2
    tick(panel)
    assert len(panel.callbacks) == 1
    dispatch(panel)
    assert panel.captures == [(2, False)]


def test_timeline_and_new_parameter_edits_share_complete_capture(panel):
    panel.panel.request_update()
    panel.host.now = 2
    tick(panel)
    before = panel.panel._update_serial
    panel.panel.request_update()
    assert panel.panel._update_serial == before + 1
    assert panel.panel._pending
    dispatch(panel)
    assert panel.captures == [(2, True)] and not panel.panel._pending


@pytest.mark.parametrize("gate", ["pause", "auto"])
def test_queued_automatic_dispatch_rechecks_controls(panel, gate):
    panel.host.now = 2
    tick(panel)
    assert len(panel.callbacks) == 1
    if gate == "pause":
        panel.panel.entry.paused = True
    else:
        panel.panel.auto.checked = False
    dispatch(panel)
    assert not panel.captures, "Posted automatic work ignored a later control change"
    assert panel.panel._pending and panel.panel._timeline_pending_time == 2


def test_queued_automatic_work_is_cancelled_by_stop(panel):
    panel.host.now = 2
    tick(panel)
    panel.panel.cancel()
    assert not panel.panel._render_queued and not panel.panel._pending
    dispatch(panel)
    assert not panel.captures
    panel.host.now = 3
    tick(panel)
    assert not panel.callbacks


def test_manual_refresh_retains_priority_over_automatic_gates(panel):
    panel.panel.queue_render(reuse_if_unchanged=False)
    panel.panel.auto.checked = False
    panel.panel.entry.paused = True
    dispatch(panel)
    assert panel.captures == [(1.0, False)]
