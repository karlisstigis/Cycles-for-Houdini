# SPDX-License-Identifier: Apache-2.0
"""No Houdini/GPU required: exercise real suspension ownership and Stop precedence."""

from pathlib import Path
from types import SimpleNamespace
import runpy

Coordinator = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/ipr/ipr_suspend.py")
)["LocalFinalSuspension"]


class Session:
    def __init__(self, state="rendering"):
        self.state = state
        self.calls = []

    def status(self):
        return {"state": self.state}

    def pause(self, value):
        self.calls.append(value)
        self.state = "paused" if value else "rendering"


def entry(name, state="rendering", paused=False):
    return SimpleNamespace(key=name, session=Session(state), paused=paused, final_paused=False)


c = Coordinator()
a = entry("shared")
b = entry("other")
manual = entry("manual", "paused", True)
complete = entry("complete", "complete")
stopped = entry("stopped", "cancelled")
first = c.begin([a, b, manual, complete, stopped])
second = c.begin([a, b, manual])
assert a.session.calls == [True] and b.session.calls == [True]
assert not manual.session.calls and not complete.session.calls and not stopped.session.calls
assert c.holds(a) and c.holds(complete)
c.end(first)
assert c.active and a.session.state == "paused"
b.session.state = "cancelled"  # Stop in a second panel during the final.
a.paused = True  # A deliberate user pause remains after the final.
c.end(second)
assert not c.active and not a.final_paused and not b.final_paused
assert a.session.calls == [True] and b.session.calls == [True]
# Completion, failure/cancel and launch rollback all use the same final release.
a.paused = False
a.session.state = "rendering"
for reason in ("completed", "failed", "cancelled", "launch-error"):
    token = c.begin([a])
    c.end(token)
    assert a.session.state == "rendering" and not a.final_paused, reason
# Backend replacement and closed workers must not be restarted.
token = c.begin([a])
original = a.session
a.session = Session("idle")
c.end(token)
assert original.calls[-1] is True and not a.session.calls
a.session = Session("rendering")
token = c.begin([a])
a.session.state = "closed"
c.end(token)
assert a.session.calls == [True]
# Worker Stop is asynchronous: the cached backend can still say paused when
# the final completes. Preserve the explicit host Stop intent immediately.
a.session = Session("rendering")
a.stop_serial = 0
token = c.begin([a])
a.stop_serial += 1
c.end(token)
assert a.session.calls == [True], a.session.calls
print(
    "Passed: nested finals/shared panels, user Pause/Stop including queued Stop, rollback, replacement and closed-worker precedence"
)
