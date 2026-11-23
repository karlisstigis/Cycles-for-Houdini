# SPDX-License-Identifier: Apache-2.0
"""Actual Viewer memory polling stays idle without a visible usable session."""

import ast
from support.paths import product_source
from support.package_loader import prepare_package
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

source = product_source("panel.py")
tree = ast.parse(source.read_text(encoding="utf-8"))
klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RenderPanel")
method = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "poll_memory")
calls = []
package = ModuleType("viewer_memory_idle")
package.devices = lambda: calls.append("devices") or []
package.viewer_memory = SimpleNamespace(
    describe=lambda *args: ("details", "scope"), compact=lambda *args: "Mem: sample"
)
prepare_package(package)
ns = dict(
    __package__=package.__name__ + ".viewer",
    time=SimpleNamespace(monotonic=lambda: 123.0),
    process_memory=lambda: calls.append("host_gpu") or {},
)
exec(compile(ast.Module(body=[method], type_ignores=[]), "<actual memory poll>", "exec"), ns)


class Label:
    def __init__(self):
        self.text = "retained"
        self.tip = "retained"
        self.visible = False

    def setText(self, value, *, tooltip=None):
        self.text = value
        self.tip = value if tooltip is None else tooltip

    def setVisible(self, value):
        self.visible = value


def panel(state="rendering", *, visible=True, enabled=True, empty=False, rejected=False):
    def status():
        calls.append("status")
        return dict(
            state="failed" if rejected else state,
            backend_state=state,
            update_rejected=rejected,
            render_device_ids=["CPU"],
        )

    session = SimpleNamespace(status=status, memory_snapshot=lambda: calls.append("renderer") or {})
    return SimpleNamespace(
        _disposed=False,
        memory_details=SimpleNamespace(isChecked=lambda: enabled),
        memory_info=Label(),
        entry=None if empty else SimpleNamespace(session=session),
        isVisible=lambda: visible,
        _last_memory_poll=0.0,
    )


def poll(p, force=False):
    calls.clear()
    ns["poll_memory"](p, force)
    return list(calls)


for p in (panel(empty=True), panel(visible=False), panel(enabled=False)):
    assert poll(p) == [] and poll(p, True) == [], calls
for state in ("cancelled", "idle", "failed", "closing", "closed"):
    p = panel(state)
    assert poll(p) == ["status"] and p.memory_info.text == "Mem: —"
for state in ("rendering", "paused", "complete"):
    p = panel(state)
    assert poll(p) == ["status", "host_gpu", "renderer", "devices"] and p.memory_info.text == "Mem: sample"
    assert poll(p) == [], "The one-second interval stopped coalescing queries"
    assert poll(p, True) == ["status", "host_gpu", "renderer", "devices"]
p = panel("paused", rejected=True)
assert poll(p) == ["status", "host_gpu", "renderer", "devices"]
# A hidden paused/completed pane resumes ordinary sampling as soon as shown.
for state in ("paused", "complete"):
    p = panel(state, visible=False)
    assert poll(p) == []
    p.isVisible = lambda: True
    assert poll(p) == ["status", "host_gpu", "renderer", "devices"]
# Scene replacement clears the old readout and then samples only the new owner.
p = panel()
poll(p)
p.entry = None
assert poll(p) == [] and p.memory_info.text == "Mem: —"
new = panel("complete")
p.entry = new.entry
assert poll(p, True) == ["status", "host_gpu", "renderer", "devices"]
p._disposed = True
assert poll(p, True) == []
print(
    "Passed: empty/hidden/Stopped memory idle; visible paused/completed, pane return, rejection and scene replacement preserved"
)

# Use the actual FinalStream status adapter, rather than assuming its states
# match native IPR. Cached active states and terminal completion stay eligible.
final_tree = ast.parse(product_source("final_stream.py").read_text(encoding="utf-8"))
remote = next(n for n in final_tree.body if isinstance(n, ast.ClassDef) and n.name == "RemoteSession")
status_method = next(n for n in remote.body if isinstance(n, ast.FunctionDef) and n.name == "status")
final_ns = {}
exec(compile(ast.Module(body=[status_method], type_ignores=[]), "<actual final stream status>", "exec"), final_ns)
for state, job, eligible in [
    ("rendering", "rendering", True),
    ("paused", "rendering", True),
    ("rendering", "completed", True),
    ("rendering", "cancelled", False),
    ("rendering", "failed", False),
]:
    session = SimpleNamespace(
        lock=threading.Lock(),
        cached=dict(state=state, generation=1, render_device_ids=["CPU"]),
        job_value=dict(state=job),
        closing=threading.Event(),
        transport_error="",
        memory_snapshot=lambda: calls.append("renderer") or {},
    )
    session.status = lambda: final_ns["status"](session)
    p = panel()
    p.entry.session = session
    actual = poll(p)
    assert actual == (["host_gpu", "renderer", "devices"] if eligible else []), (state, job, actual)
# The error path uses the same combined StatusLine API as sampled/idle text.
p = panel()
saved = ns["process_memory"]


def unavailable():
    raise RuntimeError("probe unavailable")


ns["process_memory"] = unavailable
poll(p)
assert p.memory_info.text == "Mem: N/A" and p.memory_info.tip == "Process memory unavailable: probe unavailable"
ns["process_memory"] = saved
print("Passed actual FinalStream rendering/paused/completed eligibility and combined text/tooltip updates")
