# SPDX-License-Identifier: Apache-2.0
"""Render warnings of a finished sequence: in the Houdini interface they go to the status bar and are
never printed (printing opens the Houdini console over the artist's work); without the interface
(hython, background renders) every line is printed to the render log. Runs under hython; the
interface is simulated by replacing the module's hou."""

import contextlib, io, json
from types import SimpleNamespace

import hou
from cycles_houdini import final_render
from support.paths import artifacts_root

out = artifacts_root() / "stage2/warnings"
out.mkdir(parents=True, exist_ok=True)
warned = {
    (frame, "motion.missing_velocity", "/obj/a", "", "v"): "Cycles warning [x]: frame %d" % frame for frame in (1, 2, 3)
}
state = SimpleNamespace(warned=dict(warned))

log = io.StringIO()
with contextlib.redirect_stdout(log):
    final_render._report_warnings(state)
headless = log.getvalue().splitlines()
assert headless == list(warned.values()), headless

messages = []
real = final_render.hou
final_render.hou = SimpleNamespace(
    isUIAvailable=lambda: True,
    ui=SimpleNamespace(setStatusMessage=lambda text, severity=None: messages.append((text, severity))),
    severityType=SimpleNamespace(Warning="warning"),
)
try:
    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        final_render._report_warnings(state)
        final_render._report_warnings(SimpleNamespace(warned=None))
finally:
    final_render.hou = real
assert console.getvalue() == "", console.getvalue()
assert messages == [("Cycles warning [x]: frame 3", "warning")], messages

(out / "warning-console.json").write_text(
    json.dumps(dict(result="passed", headless=headless, status_bar=messages)), encoding="utf-8"
)
print("warning console smoke passed")
