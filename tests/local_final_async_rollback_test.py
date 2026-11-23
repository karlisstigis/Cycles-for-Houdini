# SPDX-License-Identifier: Apache-2.0
import runpy
from pathlib import Path
import ast
from support.paths import product_source
from types import SimpleNamespace

source = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini"
C = runpy.run_path(str(source / "ipr/ipr_suspend.py"))["LocalFinalSuspension"]


class Delayed:
    def __init__(self):
        self.state = "rendering"
        self.pending = []
        self.calls = []

    def status(self):
        return dict(state=self.state)

    def pause(self, v):
        self.pending.append(("pause", v))
        self.calls.append(v)

    def stop(self):
        self.pending.append(("stop", None))

    def flush(self):
        for k, v in self.pending:
            self.state = "cancelled" if k == "stop" else ("paused" if v else "rendering")
        self.pending = []


class Entry:
    key = "ipr"
    paused = False
    final_paused = False
    stop_serial = 0

    def __init__(self):
        self.session = Delayed()


def test(stop=False, user_pause=False, overlap=False):
    c = C()
    e = Entry()
    a = c.begin([e])
    b = c.begin([e]) if overlap else None
    assert e.session.state == "rendering" and e.session.calls == [True]
    if stop:
        e.stop_serial += 1
        e.session.stop()
    if user_pause:
        e.paused = True
    c.end(a)
    if overlap:
        assert e.session.calls == [True]
        c.end(b)
    e.session.flush()
    assert e.session.state == ("cancelled" if stop else "paused" if user_pause else "rendering"), (
        stop,
        user_pause,
        overlap,
        e.session.calls,
        e.session.state,
    )


for overlap in (False, True):
    test(overlap=overlap)
    test(stop=True, overlap=overlap)
    test(user_pause=True, overlap=overlap)
print(
    "Passed delayed pause acknowledgement: immediate rollback, overlapping leases, genuine queued Stop and user Pause"
)

# Actual orphan-view cleanup must close failed registrations, while a new
# running FinalView remains alive during _attach()'s initial _detach().
tree = ast.parse(product_source("panel.py").read_text(encoding="utf-8"))
klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RenderPanel")
method = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "_detach")
ns = dict(registry=SimpleNamespace(detach=lambda entry: None))
exec(compile(ast.Module(body=[method], type_ignores=[]), "<actual panel detach>", "exec"), ns)
for running in (False, True):
    closes = []
    view = SimpleNamespace(running=running, close=lambda: closes.append("close"))
    panel = SimpleNamespace(entry=None, _final_view=view, watch_frames=lambda value: None)
    ns["_detach"](panel)
    assert closes == ([] if running else ["close"]) and (
        panel._final_view is view if running else panel._final_view is None
    )
print("Passed orphan registration retirement and new running FinalView preservation")
