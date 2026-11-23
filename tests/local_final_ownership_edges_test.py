# SPDX-License-Identifier: Apache-2.0
"""Execute real ownership methods at HIP, constructor, registration and deferred Play edges."""

import ast
from support.paths import product_source
from support.package_loader import load_module, prepare_package
from pathlib import Path
import runpy
import sys
import time
from types import ModuleType, SimpleNamespace

source = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini"
Coordinator = runpy.run_path(str(source / "ipr/ipr_suspend.py"))["LocalFinalSuspension"]


class Session:
    def __init__(self, state="rendering"):
        self.state = state
        self.calls = []

    def status(self):
        return dict(state=self.state, generation=1)

    def pause(self, value):
        self.calls.append(value)
        self.state = "paused" if value else "rendering"

    def close(self):
        self.state = "closed"


def entry(key="ipr", state="rendering"):
    return SimpleNamespace(key=key, session=Session(state), paused=False, final_paused=False, stop_serial=0, views=1)


def functions(filename, names, class_name=None):
    tree = ast.parse(product_source(filename).read_text(encoding="utf-8"))
    body = (
        next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name).body
        if class_name
        else tree.body
    )
    return ast.Module(
        body=[node for node in body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]
    )


# Actual registry scene cleanup preserves process-owned jobs and lets the new
# HIP retain fresh entries without resuming sessions that never started.
c = Coordinator()
old = entry()
token = c.begin([old])
job = SimpleNamespace(state="rendering", stop=lambda: None)
jobs = {token: job}
entries = {"ipr": old}
polls = []
registry_ns = dict(
    _local_finals=c,
    _local_final_jobs=jobs,
    _entries=entries,
    _bridge=None,
    _polling=False,
    _registered_shutdown=False,
    ensure_polling=lambda: polls.append("poll"),
    retire=lambda key: entries.pop(key).session.close(),
)
exec(compile(functions("registry.py", {"close_all"}), "<registry ownership>", "exec"), registry_ns)
registry_ns["close_all"](preserve_local_finals=True)
assert c.active and token in jobs and old.session.state == "closed" and polls
new = entry("new-hip", "idle")
c.retain_entry(new)
assert c.holds(new)
c.end(token)
assert not c.active and not new.session.calls and new.session.state == "idle"

# Compile the actual panel methods so failures cannot hide behind a mock copy
# of the ownership algorithm. UI plumbing alone is replaced by minimal fakes.
package = ModuleType("ownership_edges")
package.__path__ = [str(source)]
sys.modules[package.__name__] = package
viewer_module = ModuleType(package.__name__ + ".viewer_final")
sys.modules[viewer_module.__name__] = viewer_module


class View:
    running = False

    def __init__(self, panel, rop, **kwargs):
        if failure[0] == "constructor":
            raise ValueError("constructor failure")
        self.panel = panel
        self.previous_ipr = None
        self.job = None

    def close(self):
        if self.previous_ipr is not None:
            registry.detach(self.previous_ipr)
            self.previous_ipr = None

    def run(self):
        raise ValueError("registration failure")

    def poll(self):
        pass


viewer_module.FinalView = View
failure = ["constructor"]
c = Coordinator()
e = entry()
registry = SimpleNamespace(
    begin_local_final=lambda: c.begin([e]),
    attach=lambda value: setattr(value, "views", value.views + 1),
    detach=lambda value: setattr(value, "views", value.views - 1),
    abandon_local_final=lambda token: c.end(token),
    local_final_active=lambda: c.active,
    ensure_polling=lambda: None,
)
prepare_package(package)
panel_ns = dict(__package__=package.__name__ + ".viewer", registry=registry, time=time)
exec(
    compile(
        functions("panel.py", {"_run_final_render", "render", "poll", "cancel"}, "RenderPanel"),
        "<panel ownership>",
        "exec",
    ),
    panel_ns,
)


def panel():
    p = SimpleNamespace(
        _final_queued=True,
        _disposed=False,
        _rop=object(),
        _final_view=None,
        entry=e,
        _deferred_render=False,
        _stopped=False,
        _next_scale_poll=float("inf"),
        requests=0,
        queued=0,
        snapshot_compare=SimpleNamespace(setChecked=lambda value: None),
        snapshot_menu=SimpleNamespace(setCurrentIndex=lambda value: None),
    )
    p.notify = lambda value: None
    # Viewer.poll does no work while hidden (commit 3a45b76): this panel is shown.
    p.ui_active = lambda: True
    p.update_render_controls = lambda: None
    p.poll_memory = lambda: None
    p.finish_pending_save = lambda: None
    p.request_update = lambda: setattr(p, "requests", p.requests + 1)
    p.queue_render = lambda: setattr(p, "queued", p.queued + 1)

    def detach():
        if failure[0] == "detach":
            raise ValueError("detach failure")
        if p.entry is not None:
            registry.detach(p.entry)
        p.entry = None

    p._detach = detach
    return p


for stage in ("constructor", "detach", "registration"):
    failure[0] = stage
    c = Coordinator()
    e = entry()
    p = panel()
    panel_ns["_run_final_render"](p)
    assert not c.active and e.session.state == "rendering", (stage, c.active, e.session.state)
    assert e.views == (0 if stage == "registration" else 1), (stage, e.views)

# Play survives overlap, without changing the shared user pause flag. Stop
# cancels the deferred Play before the last job can dispatch it.
c = Coordinator()
e = entry()
e.paused = True
token = c.begin([e])
failure[0] = "none"
p = panel()
p._final_view = SimpleNamespace(running=False, poll=lambda: None)
panel_ns["render"](p)
assert p._deferred_render and not p._stopped and e.paused and p.queued == 0
panel_ns["poll"](p)
assert p.queued == 0
c.end(token)
panel_ns["poll"](p)
assert p.queued == 1 and e.paused and not e.session.calls
c = Coordinator()
e = entry()
token = c.begin([e])
p = panel()
p.entry = None
p._final_view = SimpleNamespace(running=False, poll=lambda: None)
panel_ns["render"](p)
panel_ns["cancel"](p)
c.end(token)
panel_ns["poll"](p)
assert p._stopped and not p._deferred_render and p.queued == 0

# Execute real FinalView.run with failures after creating the remote stream.
# Both a configuration failure before Entry creation and panel-registration
# failure must close that stream while retaining the child's pause lease.
fake_final = SimpleNamespace(_summary_key="summary")
remote_sessions = []
stops = []
ended = []


class Remote:
    def __init__(self, *args):
        self.closed = False
        remote_sessions.append(self)

    def close(self):
        self.closed = True


final_stream = ModuleType(package.__name__ + ".final_stream")
final_stream.RemoteSession = Remote
sys.modules[final_stream.__name__] = final_stream
background = ModuleType(package.__name__ + ".background_render")
background.launch = lambda *args, **kwargs: SimpleNamespace(directory=Path("."), stop=lambda: stops.append("stop"))
background._read = lambda path: dict(viewer={})
sys.modules[background.__name__] = background
package.background_render = background
package.final_render = fake_final
package.registry = SimpleNamespace(
    Entry=lambda *args: SimpleNamespace(session=args[2]), end_local_final=lambda token: ended.append(token)
)
package.native_scene = SimpleNamespace(configuration=lambda *args, **kwargs: ())
real = load_module(package, "viewer_final")
for stage in ("configuration", "attach"):

    def configuration(*args, **kwargs):
        if stage == "configuration":
            raise ValueError("configuration failure")
        return ()

    package.native_scene.configuration = configuration
    p = SimpleNamespace(_attach=lambda value: (_ for _ in ()).throw(ValueError("attach failure")))
    view = object.__new__(real.FinalView)
    view.panel = p
    view.rop = SimpleNamespace(setCachedUserData=lambda *args: None)
    view.pause_token = object()
    view.disposed = True
    view.job = None
    view.entry = None
    view.running = True
    view.owned = False
    view.run()
    assert view.error and remote_sessions[-1].closed and not view.owned and stops and not ended, stage
print(
    "Passed: HIP lease transfer, constructor/detach/registration rollback, deferred Play/Pause/Stop and remote stream cleanup"
)
