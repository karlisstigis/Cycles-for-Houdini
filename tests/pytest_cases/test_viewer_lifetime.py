# SPDX-License-Identifier: Apache-2.0
"""A hidden viewer does no UI work; a closed one releases its render.

The panel methods come from the owned RenderPanel class; only Qt and Houdini
surfaces are faked. Facts about which events Houdini sends are measured by
tests/houdini_viewer_lifetime_gui_smoke.py.
"""

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from support.package_loader import load_module, prepare_package

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


def load_lifetime():
    spec = importlib.util.spec_from_file_location("viewer_lifetime_under_test", VIEWER / "viewer_lifetime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Timer:
    def __init__(self, active=False):
        self.active = active
        self.starts = []

    def start(self, msec=None):
        self.active = True
        self.starts.append(msec)

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active


class Session:
    def __init__(self):
        self.feedback = []
        self.pauses = []
        self.calls = 0

    def set_feedback(self, viewer, enabled):
        self.feedback.append(enabled)

    def pause(self, paused):
        self.pauses.append(paused)

    def __getattr__(self, name):
        raise AssertionError("hidden viewer touched the session: " + name)


class HouError(Exception):
    pass


class PaneTab:
    def __init__(self, kind="python", widget=None):
        self.kind = kind
        self.widget = widget
        self.reloads = 0

    def activeInterfaceRootWidget(self):
        self.type()
        return self.widget

    def reloadActiveInterface(self):
        self.reloads += 1

    def type(self):
        if self.kind is None:
            raise HouError("ObjectWasDeleted")
        return self.kind


METHODS = {
    "_host_pane_tabs",
    "_find_host_tab",
    "_revive",
    "_retry_revive",
    "set_host_pane_tab",
    "ui_active",
    "update_lifetime",
    "_enter_hidden",
    "_enter_visible",
    "_resume_poll",
    "_schedule_host_check",
    "check_host",
    "poll",
    "present_available",
    "request_camera_update",
    "queue_pose_preview",
    "_run_queued_render",
    "watch_frames",
    "follow_display",
}


def make_panel(lifetime, policy="finish"):
    source = VIEWER / "panel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RenderPanel")
    body = [node for node in definition.body if isinstance(node, ast.FunctionDef) and node.name in METHODS]
    assert {node.name for node in body} == METHODS
    extracted = ast.ClassDef(name="Panel", bases=[], keywords=[], body=body, decorator_list=[])
    posted = []
    tabs = []
    hou = SimpleNamespace(
        paneTabType=SimpleNamespace(PythonPanel="python"),
        Error=HouError,
        isUIAvailable=lambda: True,
        ui=SimpleNamespace(postEventCallback=posted.append, paneTabs=lambda: tabs, floatingPanels=lambda: []),
    )
    qt = SimpleNamespace(Slot=lambda: (lambda f: f), QTimer=SimpleNamespace(singleShot=lambda ms, f: posted.append(f)))
    log = []
    messages = []
    namespace = dict(
        hou=hou,
        viewer_lifetime=lifetime,
        QtCore=qt,
        shiboken6=SimpleNamespace(isValid=lambda w: True),
        session_log=SimpleNamespace(write=lambda event, **fields: log.append((event, fields))),
    )
    module = ast.fix_missing_locations(ast.Module(body=[extracted], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    panel = namespace["Panel"]()
    panel._disposed = False
    panel._presenting = False
    panel._final_view = None
    panel._lifetime = lifetime.Lifetime(policy)
    panel._host_tab = None
    panel._hidden_paused = None
    panel.timer = Timer(active=True)
    panel._liveness_timer = Timer()
    panel._resume_timer = Timer()
    panel._status_key = ("rendering",)
    panel._status_due = 99.0
    panel._last_memory_poll = 50.0
    window = SimpleNamespace(minimized=False)
    window.isMinimized = lambda: window.minimized
    panel.canvas = SimpleNamespace(visible=True, window=lambda: window)
    panel.canvas.isVisible = lambda: panel.canvas.visible
    panel.window_state = window
    panel.shortcuts = []
    panel._shortcuts = SimpleNamespace(set_enabled=panel.shortcuts.append)
    panel.watched = []
    panel.watch_frames = panel.watched.append
    panel.disposed = []
    panel.dispose = lambda: panel.disposed.append(True)
    panel.session = Session()
    panel.entry = SimpleNamespace(session=panel.session, views=1, channel="", paused=False, final_paused=False)
    panel.polls = []
    panel.posted = posted
    panel.tabs = tabs
    panel._revive_posted = False
    panel.log = log
    panel.messages = messages
    panel.info = SimpleNamespace(setText=messages.append)
    panel.renders = []
    panel.updates = []
    panel.render = lambda **kwargs: panel.renders.append(kwargs)
    panel.request_update = lambda **kwargs: panel.updates.append(kwargs)
    panel._camera_update_queued = panel._pose_preview_queued = panel._in_render = False
    panel._render_queued = panel._force_render_pending = panel._stopped = False
    panel._render_mode = True
    panel._run_safe_pose_preview = lambda: None
    panel._run_queued_pose_preview = panel._queue_camera_update = lambda: None
    panel.auto = SimpleNamespace(isChecked=lambda: True)
    return panel


@pytest.fixture
def lifetime():
    return load_lifetime()


def hide(panel):
    panel.canvas.visible = False
    panel.update_lifetime()


def show(panel):
    panel.canvas.visible = True
    panel.update_lifetime()


def test_hidden_viewer_stops_polling_conversion_and_key_filter_but_keeps_rendering(lifetime):
    panel = make_panel(lifetime)
    hide(panel)
    assert panel._lifetime.state == lifetime.HIDDEN
    assert not panel.timer.active and not panel._resume_timer.active
    assert panel.watched == [None]  # No worker frame notifications, so no display conversion.
    assert panel.session.feedback == [False]
    assert panel.shortcuts == [False]
    assert panel.session.pauses == []  # "finish": the render in progress continues.
    assert not panel._liveness_timer.active  # No pane tab known: nothing runs while hidden.


def test_hidden_viewer_poll_and_present_do_no_work(lifetime):
    panel = make_panel(lifetime)
    hide(panel)
    panel.timer.active = True  # A stray start must not keep a 60 Hz poll alive.
    panel.poll()
    assert not panel.timer.active
    panel.present_available()  # Session fake raises on any other access.
    assert not panel._presenting


def test_showing_resumes_at_once_with_current_session(lifetime):
    panel = make_panel(lifetime)
    hide(panel)
    show(panel)
    assert panel._lifetime.state == lifetime.VISIBLE
    assert panel.timer.active and panel._resume_timer.active
    assert panel.watched == [None, panel.session]
    assert panel.shortcuts == [False, True]
    assert panel._status_key is None and panel._status_due == 0.0 and panel._last_memory_poll == 0
    panel.poll = lambda: panel.polls.append(True)
    panel._resume_poll()
    assert panel.polls == [True]


def test_repeated_events_do_not_repeat_transitions(lifetime):
    panel = make_panel(lifetime)
    show(panel)  # Activation hook while already visible.
    assert panel.watched == [] and panel.shortcuts == []
    hide(panel)
    hide(panel)  # Hide event followed by onDeactivateInterface.
    assert panel.watched == [None] and panel.shortcuts == [False]


def test_minimised_window_is_hidden_and_fullscreen_canvas_is_visible(lifetime):
    panel = make_panel(lifetime)
    panel.window_state.minimized = True  # Floating panel minimised: isVisible() stays True.
    panel.update_lifetime()
    assert panel._lifetime.state == lifetime.HIDDEN
    panel.window_state.minimized = False
    panel.update_lifetime()
    assert panel._lifetime.state == lifetime.VISIBLE
    # Image-only fullscreen: the panel may be hidden, but the canvas is on screen.
    panel.update_lifetime()
    assert panel._lifetime.state == lifetime.VISIBLE and panel.timer.active


def test_disposed_viewer_ignores_visibility(lifetime):
    panel = make_panel(lifetime)
    panel._disposed = True
    hide(panel)
    assert panel._lifetime.state == lifetime.VISIBLE and panel.watched == []


@pytest.mark.parametrize("gone", ["type_changed", "deleted"])
def test_hidden_viewer_whose_pane_tab_is_gone_is_disposed(lifetime, gone):
    panel = make_panel(lifetime)
    tab = PaneTab()
    panel.set_host_pane_tab(tab)
    hide(panel)
    assert panel._liveness_timer.starts == [0]
    tab.kind = "pythonshell" if gone == "type_changed" else None
    panel.check_host()
    assert panel.disposed == [True]


def test_pane_tab_showing_another_interface_keeps_the_viewer(lifetime):
    # Houdini shows the same widget again when the Python Panel switches back.
    panel = make_panel(lifetime)
    panel.set_host_pane_tab(PaneTab("python"))
    hide(panel)
    for _ in range(5):
        panel.check_host()
    assert panel.disposed == []
    assert panel._liveness_timer.starts == [0, 250, 750, 2000, 2000, 2000]


def test_liveness_checks_stop_when_shown(lifetime):
    panel = make_panel(lifetime)
    panel.set_host_pane_tab(PaneTab(None))
    hide(panel)
    show(panel)
    assert not panel._liveness_timer.active
    panel.check_host()  # A queued check after showing must not dispose.
    assert panel.disposed == []


def test_pane_tab_given_while_hidden_schedules_a_check(lifetime):
    panel = make_panel(lifetime)
    hide(panel)
    panel.set_host_pane_tab(PaneTab())
    assert panel._liveness_timer.starts == [0]


def test_pause_policy_pauses_only_an_unshared_session_and_resumes_it(lifetime):
    panel = make_panel(lifetime, policy="pause")
    hide(panel)
    assert panel.session.pauses == [True]
    show(panel)
    assert panel.session.pauses == [True, False]
    panel.entry.final_paused = True
    hide(panel)
    assert panel.session.pauses == [True, False]  # Already held by a local final.


@pytest.mark.parametrize("shared", ["views", "native_ipr", "user_paused", "final_view"])
def test_pause_policy_never_pauses_shared_or_user_state(lifetime, shared):
    panel = make_panel(lifetime, policy="pause")
    if shared == "views":
        panel.entry.views = 2
    elif shared == "native_ipr":
        panel.entry.channel = "control"
    elif shared == "user_paused":
        panel.entry.paused = True
    else:
        panel._final_view = object()
    hide(panel)
    show(panel)
    assert panel.session.pauses == []


def test_user_pause_while_hidden_is_not_undone_on_show(lifetime):
    panel = make_panel(lifetime, policy="pause")
    hide(panel)
    panel.entry.paused = True
    show(panel)
    assert panel.session.pauses == [True]


def test_lifetime_schedule_and_policy(lifetime):
    state = lifetime.Lifetime()
    assert state.policy == lifetime.HIDDEN_RENDER == "finish"
    assert state.next_check_ms() is None
    assert state.update(False) == "hide"
    assert [state.next_check_ms() for _ in range(5)] == [0, 250, 750, 2000, 2000]
    assert state.update(True) == "show" and state.next_check_ms() is None
    state.update(False)
    assert state.next_check_ms() == 0  # Each hide starts the schedule again.
    state.close()
    assert state.update(True) is None and state.state == lifetime.CLOSED
    with pytest.raises(ValueError):
        lifetime.Lifetime("live")


def test_pane_tab_holds(lifetime):
    assert lifetime.pane_tab_holds(None, "python")
    assert lifetime.pane_tab_holds(PaneTab("python"), "python")
    assert not lifetime.pane_tab_holds(PaneTab("pythonshell"), "python")
    assert not lifetime.pane_tab_holds(PaneTab(None), "python", (HouError,))


def test_canvas_visible(lifetime):
    window = SimpleNamespace(isMinimized=lambda: False)
    assert lifetime.canvas_visible(SimpleNamespace(isVisible=lambda: True, window=lambda: window))
    assert not lifetime.canvas_visible(SimpleNamespace(isVisible=lambda: False, window=lambda: window))
    minimised = SimpleNamespace(isMinimized=lambda: True)
    assert not lifetime.canvas_visible(SimpleNamespace(isVisible=lambda: True, window=lambda: minimised))


# Registry: the host poll stops once nothing needs it --------------------------------------------


class Signal:
    def __init__(self):
        self.connected = []

    def connect(self, slot):
        self.connected.append(slot)

    def disconnect(self, slot):
        self.connected.remove(slot)


@pytest.fixture
def registry(monkeypatch):
    callbacks = []
    quit_signal = Signal()
    app = SimpleNamespace(aboutToQuit=quit_signal)
    qt = ModuleType("PySide6")
    qt.QtWidgets = SimpleNamespace(QApplication=SimpleNamespace(instance=lambda: app))
    qt.QtCore = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "PySide6", qt)
    hou = SimpleNamespace(
        isUIAvailable=lambda: True,
        ObjectWasDeleted=type("ObjectWasDeleted", (Exception,), {}),
        ui=SimpleNamespace(
            eventLoopCallbacks=lambda: tuple(callbacks),
            addEventLoopCallback=callbacks.append,
            removeEventLoopCallback=callbacks.remove,
        ),
        hipFile=SimpleNamespace(addEventCallback=lambda callback: None),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    package = ModuleType("test_viewer_lifetime_registry_fixture")
    package.Session = object
    package._native = SimpleNamespace()
    package.synchronize_color_configuration = lambda: None
    prepare_package(package)
    package.scene.native_scene = SimpleNamespace()
    try:
        module = load_module(package, "registry")
        monkeypatch.setattr(module, "_poll_host", lambda: None)
        yield SimpleNamespace(module=module, callbacks=callbacks, quit=quit_signal)
    finally:
        for name in tuple(sys.modules):
            if name == package.__name__ or name.startswith(package.__name__ + "."):
                sys.modules.pop(name, None)


def test_registry_stops_host_poll_after_last_session_and_restarts_on_demand(registry):
    module = registry.module
    module.ensure_polling()
    assert registry.callbacks == [module.poll] and module._polling
    module.poll()
    assert registry.callbacks == [] and not module._polling and registry.quit.connected == []
    module.ensure_polling()
    assert registry.callbacks == [module.poll] and module._polling


@pytest.mark.parametrize("busy", ["entry", "retire", "local_final"])
def test_registry_keeps_polling_while_work_remains(registry, busy, monkeypatch):
    module = registry.module
    module.ensure_polling()
    if busy == "entry":
        monkeypatch.setitem(module._entries, "rop:1", object())
    elif busy == "retire":
        module._retire_requested.add("rop:1")
    else:
        monkeypatch.setitem(module._local_final_jobs, "token", object())
    module.poll()
    assert registry.callbacks == [module.poll] and module._polling
    module._retire_requested.clear()


def test_hidden_viewer_defers_event_driven_pose_and_camera_updates(lifetime):
    # Object and camera edits arrive through the viewer's own scene observer,
    # not its poll; while hidden they only mark the update pending.
    panel = make_panel(lifetime)
    hide(panel)
    panel.request_camera_update()
    panel.queue_pose_preview()
    assert len(panel.updates) == 1 and panel.posted == []
    assert not panel._camera_update_queued and not panel._pose_preview_queued
    show(panel)
    panel.queue_pose_preview()
    assert panel._pose_preview_queued
    assert panel.posted == [panel._run_queued_pose_preview, panel._run_safe_pose_preview]


def test_automatic_render_queued_before_hiding_waits_but_manual_render_runs(lifetime):
    panel = make_panel(lifetime)
    panel._render_queued = True
    hide(panel)
    panel._run_queued_render()
    assert panel.renders == []
    panel._render_queued = panel._force_render_pending = True  # Render clicked, then tab switched.
    panel._run_queued_render()
    assert panel.renders == [dict(reuse_if_unchanged=False)]


def test_attaching_while_hidden_subscribes_and_converts_nothing(lifetime):
    # A ROP picked while hidden (onNodePathChanged) attaches without frame work.
    panel = make_panel(lifetime)
    del panel.watch_frames  # Use the real method.
    follows, wakeups = [], []
    panel._display_queue = SimpleNamespace(follow=lambda *args: follows.append(args[0]), source_ready=None)
    panel._display_epoch = 0
    panel._frame_session = None
    session = SimpleNamespace(
        set_frame_wakeup=lambda viewer, callback: wakeups.append(callback), set_feedback=lambda *args: None
    )
    panel.entry.session = session
    hide(panel)
    panel.watch_frames(session)
    panel.follow_display(session)
    assert wakeups == [] and panel._frame_session is None and follows == []
    show(panel)
    assert len(wakeups) == 1 and panel._frame_session is session


def test_visible_canvas_vetoes_release(lifetime):
    # A hide before the first show (activation hook) must not let a check
    # release a viewer that is on screen by the time the check runs.
    panel = make_panel(lifetime)
    panel.set_host_pane_tab(PaneTab("pythonshell"))  # Stale: captured mid type change.
    hide(panel)
    panel.canvas.visible = True
    panel.check_host()
    assert panel.disposed == [] and panel._lifetime.state == lifetime.VISIBLE


def test_stale_pane_tab_is_replaced_by_the_tab_showing_the_viewer(lifetime):
    panel = make_panel(lifetime)
    panel.set_host_pane_tab(PaneTab(None))
    current = PaneTab("python", widget=panel)
    panel.tabs.extend([PaneTab("python", widget=object()), current])
    hide(panel)
    panel.check_host()
    assert panel.disposed == [] and panel._host_tab is current
    current.kind = "pythonshell"  # Now really no longer a Python Panel.
    panel.check_host()
    assert panel.disposed == [True]


def test_released_viewer_shown_again_reloads_its_interface_once(lifetime):
    # viewer -> Python Shell -> Python Panel on one tab re-shows the old widget.
    panel = make_panel(lifetime)
    tab = PaneTab("python", widget=panel)
    panel.tabs.append(tab)
    panel._disposed = True
    show(panel)
    show(panel)
    assert len(panel.posted) == 1
    reload = panel.posted.pop()
    tab.widget = object()  # The tab is still being set up and shows something else.
    reload()
    assert tab.reloads == 0 and len(panel.posted) == 1  # Retries instead.
    tab.widget = panel
    panel.posted.pop()()  # Retry: posts a new reload.
    panel.posted.pop()()
    assert tab.reloads == 1


def test_released_viewer_not_on_screen_is_not_revived(lifetime):
    panel = make_panel(lifetime)
    panel.tabs.append(PaneTab("python", widget=panel))
    panel._disposed = True
    hide(panel)
    assert panel.posted == []


def test_find_pane_tab(lifetime):
    widget = object()
    tabs = [PaneTab(None), PaneTab("pythonshell", widget), PaneTab("python", object()), PaneTab("python", widget)]
    assert lifetime.find_pane_tab(tabs, widget, "python", (HouError,)) is tabs[3]
    assert lifetime.find_pane_tab(tabs[:3], widget, "python", (HouError,)) is None


def test_revive_retries_until_the_tab_reports_the_viewer(lifetime):
    panel = make_panel(lifetime)
    panel._disposed = True
    show(panel)  # The tab does not report this widget yet (type change in progress).
    assert len(panel.posted) == 1  # A retry timer, not a reload.
    tab = PaneTab("python", widget=panel)
    panel.tabs.append(tab)
    panel.posted.pop(0)()  # Retry fires.
    assert len(panel.posted) == 1
    panel.posted.pop(0)()  # Reload callback.
    assert tab.reloads == 1


def test_revive_exhaustion_explains_how_to_reopen(lifetime):
    panel = make_panel(lifetime)
    panel._disposed = True
    show(panel)
    for _ in range(30):
        panel.posted.pop(0)()
    show(panel)  # Only one terminal report for this released panel.
    assert not panel.posted
    assert panel._revive_result == "attempts exhausted"
    assert panel.messages == ["Cycles Render could not reopen. Close and reopen this pane tab."]
    assert panel.log == [("viewer could not reopen", {"attempts": 30})]
