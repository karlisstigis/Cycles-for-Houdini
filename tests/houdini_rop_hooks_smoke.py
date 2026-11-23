# SPDX-License-Identifier: Apache-2.0
"""Bounded host-script parity for HOM and in-process ROP Fetch TOP."""

from pathlib import Path
import json

import hou

from support.paths import artifacts_root
from cycles_houdini.rop import final_render


root = artifacts_root() / "rop-hooks"
root.mkdir(parents=True, exist_ok=True)
hou.playbar.setFrameRange(1, 2)
geo = hou.node("/obj").createNode("geo", "hook_geometry", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "hook_camera")
camera.parm("tz").set(5)
phases = ("prerender", "preframe", "postframe", "postrender")
success = (
    "prerender 1",
    "preframe 1",
    "postframe 1",
    "preframe 2",
    "postframe 2",
    "postrender 2",
)


def scripts(rop, name, failure=None):
    log = root / (name + ".txt")
    log.write_text("", encoding="utf-8")
    for phase in phases:
        rop.parm("l" + phase).set("python")
        code = f"import hou; open({log.as_posix()!r}, 'a').write('{phase} '+str(int(hou.frame()))+'\\n')"
        if phase == "preframe" and failure is not None:
            code += f"; raise {failure}('pre-frame sentinel')"
        rop.parm(phase).set(code)
    return log


def lines(log):
    return tuple(log.read_text(encoding="utf-8").splitlines())


def cycles(name):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(
            source_mode="sop",
            soppath=box.path(),
            camera=camera.path(),
            picture=str(root / (name + ".$F4.exr")),
            samples=1,
            resx=16,
            resy=16,
            use_denoise=0,
            adaptive_sampling=0,
            device_mode="cpu",
        )
    )
    assert all(rop.parm(name) is None for name in ("objects", "forceobjects", "excludeobjects"))
    assert all(rop.parm(name) is not None for name in ("vobject", "forceobject", "excludeobject"))
    return rop


stock = hou.node("/out").createNode("geometry", "stock_hook_reference")
stock.setParms(dict(soppath=box.path(), sopoutput=str(root / "stock.$F4.bgeo.sc"), trange=1, f1=1, f2=2, f3=1))
stock_log = scripts(stock, "stock")
stock.render(frame_range=(1, 2))
assert lines(stock_log) == success
assert all((root / f"stock.{frame:04d}.bgeo.sc").is_file() for frame in (1, 2))

rop = cycles("cycles_hooks")
log = scripts(rop, "cycles")
rop.render(frame_range=(1, 2))
assert lines(log) == lines(stock_log)
assert all((root / f"cycles_hooks.{frame:04d}.exr").is_file() for frame in (1, 2))
summary = rop.cachedUserData("cycles_houdini.final_sequence")
assert summary["completed_frames"] == 2 and not summary["session_alive"] and not summary["error"], summary
assert not final_render.active_sequences()


def failed(rop, label, exception):
    log = scripts(rop, label, failure=exception)
    try:
        rop.render(frame_range=(1, 2))
    except hou.OperationFailed as error:
        assert "pre-frame sentinel" in str(error), error
    else:
        raise AssertionError("Houdini swallowed the pre-frame failure")
    assert lines(log) == ("prerender 1", "preframe 1", "postrender 2"), lines(log)
    return rop.cachedUserData("cycles_houdini.final_sequence") if rop.type().name() == "cycles_ipr" else None


failed(stock, "stock_failure", "RuntimeError")
failure = failed(cycles("cycles_failure"), "cycles_failure", "RuntimeError")
assert failure["reason"] == "failed" and failure["error_phase"] == "pre-frame" and failure["error_frame"] == 1
assert "/out/cycles_failure" in failure["error"] and "pre-frame sentinel" in failure["error"]
cancelled = failed(cycles("cycles_cancelled"), "cycles_cancelled", "hou.OperationInterrupted")
assert cancelled["reason"] == "cancelled" and cancelled["cancelled"] and cancelled["error_frame"] == 1
assert not final_render.active_sequences()

for phase, expected_lines, frame in (
    ("postframe", ("prerender 1", "preframe 1", "postframe 1", "postrender 2"), 1),
    ("postrender", success, 2),
):
    rop = cycles("cycles_failing_" + phase)
    log = scripts(rop, "cycles_failing_" + phase)
    rop.parm(phase).set(rop.parm(phase).eval() + f"; raise RuntimeError('{phase} sentinel')")
    try:
        rop.render(frame_range=(1, 2))
    except hou.OperationFailed as error:
        assert phase + " sentinel" in str(error), error
    else:
        raise AssertionError("Houdini swallowed the " + phase + " failure")
    assert lines(log) == expected_lines, lines(log)
    summary = rop.cachedUserData("cycles_houdini.final_sequence")
    assert summary["error_phase"] == phase.replace("post", "post-") and summary["error_frame"] == frame, summary
    assert rop.path() in summary["error"] and phase + " sentinel" in summary["error"]
    assert not final_render.active_sequences()


def fetch_case(name, failure=None, target=None):
    rop = target if target is not None else cycles(name)
    log = scripts(rop, name, failure=failure)
    if target is not None:
        for frame in (1, 2):
            (root / f"cycles_hooks.{frame:04d}.exr").unlink(missing_ok=True)
    network = hou.node("/obj").createNode("topnet", name + "_tasks")
    fetch = network.createNode("ropfetch", "fetch")
    fetch.parm("roppath").set(rop.path())
    fetch.parm("pdg_cooktype").set("1")  # In-process, so this test observes the live ROP.
    fetch.parm("pdg_cachemode").set("2")  # Write Files: exercise the ROP even after the HOM output exists.
    fetch.parm("framegeneration").set("1" if target is not None else "0")
    fetch.generateStaticWorkItems(block=True)
    expected_items = 2 if target is not None else 1
    assert len(fetch.getPDGNode().workItems) == expected_items, "ROP Fetch unexpectedly scheduled frames"
    fetch.cookWorkItems(block=True)
    items = tuple(fetch.getPDGNode().workItems)
    assert not final_render.active_sequences()
    return rop, items, lines(log)


fetched, items, fetched_lines = fetch_case("cycles_fetch", target=hou.node("/out/cycles_hooks"))
assert fetched_lines == (
    "prerender 1",
    "preframe 1",
    "postframe 1",
    "postrender 1",
    "prerender 2",
    "preframe 2",
    "postframe 2",
    "postrender 2",
), fetched_lines
assert all("CookedSuccess" in str(item.state) for item in items)
assert fetched.cachedUserData("cycles_houdini.final_sequence")["completed_frames"] == 1
assert all((root / f"cycles_hooks.{frame:04d}.exr").is_file() for frame in (1, 2))

fetched, items, fetched_lines = fetch_case("cycles_fetch_failure", "RuntimeError")
assert fetched_lines == ("prerender 1", "preframe 1", "postrender 1")
assert "CookedFail" in str(items[0].state) and "pre-frame sentinel" in items[0].logMessages
assert "/out/cycles_fetch_failure" in items[0].logMessages
assert fetched.cachedUserData("cycles_houdini.final_sequence")["error_frame"] == 1
fetched, items, fetched_lines = fetch_case("cycles_fetch_cancelled", "hou.OperationInterrupted")
assert fetched_lines == ("prerender 1", "preframe 1", "postrender 1")
assert "CookedFail" in str(items[0].state) and "OperationInterrupted" in items[0].logMessages
assert fetched.cachedUserData("cycles_houdini.final_sequence")["cancelled"]
report = dict(
    result="passed",
    stock_two_frame=list(success),
    cycles_hom_two_frame=list(success),
    rop_fetch_two_frame=[
        "prerender 1",
        "preframe 1",
        "postframe 1",
        "postrender 1",
        "prerender 2",
        "preframe 2",
        "postframe 2",
        "postrender 2",
    ],
    first_error="pre-frame sentinel",
    postframe_error=dict(phase="post-frame", frame=1),
    postrender_error=dict(phase="post-render", frame=2),
    cancellation=True,
    active_sequences=len(final_render.active_sequences()),
)
(root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("ROP hooks: stock parity, HOM and two-frame ROP Fetch success, failure and cancellation passed")
