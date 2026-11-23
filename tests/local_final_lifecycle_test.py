# SPDX-License-Identifier: Apache-2.0
"""Exercise actual final ROP lifecycle hooks, including exception and headless farm paths."""

from pathlib import Path
from types import SimpleNamespace
import runpy
import farm_output_policy_test as fixtures

Coordinator = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/ipr/ipr_suspend.py")
)["LocalFinalSuspension"]


class Session:
    state = "rendering"

    def status(self):
        return {"state": self.state}

    def pause(self, value):
        self.state = "paused" if value else "rendering"


c = Coordinator()
entry = SimpleNamespace(key="ipr", session=Session(), paused=False, final_paused=False)
fixtures.package.registry.begin_local_final = lambda: c.begin([entry])
fixtures.package.registry.end_local_final = c.end
fixtures.hou.ropRenderEventType = SimpleNamespace(
    PreRender="before", PreFrame="preframe", PostFrame="postframe", PostRender="after"
)
fixtures.hou.isUIAvailable = lambda: True
final = fixtures.final
one = fixtures.Rop()
two = fixtures.Rop()
one.errors = two.errors = lambda: ()
final._render_event(one, "before", 0)
final._render_event(two, "before", 0)
assert not c.active and entry.session.state == "rendering", "PreRender alone must not suspend IPR"
final._render_event(one, "after", 0)
assert not c.active and entry.session.state == "rendering"


# Fail before scene/session creation: no PostRender is needed to release the lease.
def fail(*args):
    assert c.active and entry.session.state == "paused", "SOHO final frame must suspend IPR"
    raise ValueError("injected output validation failure")


original_plan = fixtures.output.plan
fixtures.output.plan = fail
try:
    final.render_frame(two, (), {}, 0)
except ValueError as error:
    assert str(error) == "injected output validation failure"
else:
    raise AssertionError("Failure was swallowed")
finally:
    fixtures.output.plan = original_plan
assert not c.active and entry.session.state == "rendering" and not final.active_sequences()
# A batch/farm process has no local interactive registry lease.
fixtures.hou.isUIAvailable = lambda: False
final._render_event(one, "before", 0)
assert not c.active
final._render_event(one, "after", 0)
assert not c.active
print("Passed: actual nested ROP lifecycle, validation-error release and headless farm isolation")
