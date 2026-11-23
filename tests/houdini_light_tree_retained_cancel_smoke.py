# SPDX-License-Identifier: Apache-2.0
"""Stop a real source rebuild after fast retained reuse, then repair in IPR."""

import argparse, json, os, time, traceback
from pathlib import Path
import cycles_houdini as ch
from cycles_houdini import device_selection
from stylesheet_storage_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device[0] if isinstance(device, list) else device)
if isinstance(device, list):
    device_selection.set_devices(f.rop, device)
f.styles()
dest = f.dest.parent / "lighting-transfer"
dest.mkdir(exist_ok=True)
path = dest / "cancel-report.json"
report = dict(result="running", device=device, scope=__doc__, events=[])
previous = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
session = None
log_path = None


def records(marker):
    if not log_path:
        return []
    return [
        json.loads(line[len(marker) :])
        for line in Path(log_path).read_text(errors="replace").splitlines()
        if line.startswith(marker)
    ]


def until(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        state = session.status()
        assert state["state"] != "failed", state
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError("Retained lighting cancellation stalled: " + str(state))
        time.sleep(0.001)


def render(label, x):
    global log_path
    f.obj.parm("tx").set(x)
    start = time.perf_counter()
    frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True), timeout=240)
    status = session.status()
    f.check(frame, f.offset)
    assert not status["error"] and not status["source_diagnostics"], status
    diagnostics = session.diagnostics()
    log_path = diagnostics["controller"]["worker_log"] or log_path
    event = dict(
        label=label,
        status=status,
        seconds=time.perf_counter() - start,
        prototype_cooks=f.prototype.cookCount(),
        diagnostics=diagnostics,
    )
    report["events"].append(event)
    path.write_text(json.dumps(report, indent=2))
    return event


try:
    session = ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        threads=2,
        execution="worker",
        collect_diagnostics=True,
        use_texture_cache=False,
    )
    render("Cold scene", 0)
    render("Prepare retained context", 0.03)
    render("Retained interiors are active", 0.06)
    assert any(p.get("retained_cached_nodes", 0) > 0 for p in records("CH_LIGHT_TREE_PHASES ")), records(
        "CH_LIGHT_TREE_PHASES "
    )
    starts = len(records("CH_LIGHT_TREE_BEGIN "))
    phases = len(records("CH_LIGHT_TREE_PHASES "))
    plans = len(records("CH_LIGHT_TREE_SOURCE_PLAN "))
    # Fast borrowed-source preparation can complete before the host receives its
    # marker. Interrupt real changed-emission construction, without injecting a
    # delay or requiring an already completed transaction to become invalid.
    f.styles(offset=0.04)
    f.obj.parm("tx").set(0.12)
    generation = session.apply(f.update(), reuse_if_unchanged=True)
    until(lambda: any(p["dense_emitters"] > 1000000 for p in records("CH_LIGHT_TREE_SOURCE_PLAN ")[plans:]))
    start = time.perf_counter()
    session.cancel()
    until(lambda: session.status()["state"] == "cancelled")
    until(lambda: len(records("CH_LIGHT_TREE_PHASES ")) > phases)
    interrupted = records("CH_LIGHT_TREE_PHASES ")[phases:]
    assert any(not p["complete"] and not p["buffers_valid"] for p in interrupted), interrupted
    report["interruption"] = dict(
        generation=generation, seconds=time.perf_counter() - start, phases=interrupted, status=session.status()
    )
    phases = len(records("CH_LIGHT_TREE_PHASES "))
    render("Restart restores completed buffer ownership", 0.15)
    restored = records("CH_LIGHT_TREE_PHASES ")[phases:]
    report["restart_phases"] = restored
    assert any(
        p["complete"] and p["buffers_valid"] and p["nodes_upload_requested"] and p["emitters_upload_requested"]
        for p in restored
    ), restored
    render("Prepare current cached context after source repair", 0.18)
    render("Retained reuse resumes after repair", 0.21)
    assert records("CH_LIGHT_TREE_PHASES ")[-1]["retained_cached_nodes"] > 0
    assert len({r["prototype_cooks"] for r in report["events"]}) == 1
    session.close(wait=True)
    report.update(result="passed", closed=session.status())
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close(wait=True)
    if previous is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = previous
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", events=len(report["events"]))))
