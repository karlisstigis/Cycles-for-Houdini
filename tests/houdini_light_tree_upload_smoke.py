# SPDX-License-Identifier: Apache-2.0
"""Retained lighting uploads, interrupted flattening and immediate buffer retirement.

Uses four shared million-triangle copies. Timing observations are not physical
input latency. Fresh-reference illumination and linking have separate suites.
"""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, hashlib, json, os, time, traceback
from pathlib import Path
import hou
import cycles_houdini as ch
from cycles_houdini import object_properties
from pointiness_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/performance/light-tree-uploads"
dest.mkdir(parents=True, exist_ok=True)
path = dest / "report.json"
f = Fixture(device)
f.width, f.height = 160, 90
configure_render(
    f.rop,
    dict(
        resx=f.width,
        resy=f.height,
        samples=4,
        ipr_samples=4,
        pass_position=0,
        pass_normal=0,
        limit_subdivision=True,
        subdivision_limit=2000000,
    ),
)
grid = f.obj.createNode("grid", "upload_control_mesh")
pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=181, cols=181))
grid.parmTuple("size").set((2, 2))
f.wave.addSpareParmTuple(hou.FloatParmTemplate("value", "Value", 1, default_value=(0.1,)))
f.wave.setInput(0, grid)
pass_fixture.parm(f.wave, "snippet").set('@P.z=.1*sin(3*@P.x)*cos(2*@P.y); f@rand=chf("value");')
pass_fixture.parm(f.output, "input").set(1)
object_properties.install(f.obj)
pass_fixture.set_parameters(
    f.obj,
    dict(
        cycles_subdivision_override=1,
        cycles_subdivision_type="catmull_clark",
        cycles_subdivision_adaptive=0,
        cycles_subdivision_ipr_level=2,
        cycles_subdivision_render_level=2,
    ),
)
pass_fixture.parm(f.user, "attribute").set("rand")
f.shade("user")
f.displace(False)
report = dict(
    result="running",
    device=device,
    scope=__doc__,
    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    events=[],
    interruptions=[],
)
previous = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
previous_ranges = os.environ.get("CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_TREE_RANGES")
# Keep a real, sufficiently long partial-row preparation window. The optimized
# path has its own cancellation test before cache preparation completes.
os.environ["CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_TREE_RANGES"] = "1"
session = None
log_path = None
buffers = ("light_tree_nodes", "light_tree_emitters")


def records(marker):
    if log_path is None:
        return []
    return [
        json.loads(line.split(" ", 1)[1])
        for line in Path(log_path).read_text(errors="replace").splitlines()
        if line.startswith(marker + " ")
    ]


def until(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        status = session.status()
        assert status["state"] != "failed", status
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError("Light-tree upload lifecycle stalled: " + str(status))
        time.sleep(0.001)


def render(label, uploaded=None, value=None):
    global log_path
    frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True), timeout=120)
    status = session.status()
    diagnostics = session.diagnostics()
    log_path = diagnostics["controller"]["worker_log"] or log_path
    assert not status["source_diagnostics"] and status["geometry_resources"] == 1 and status["object_resources"] == 4, (
        status
    )
    f.gray(frame, pass_fixture.evaluate(f.wave, "value") if value is None else value)
    copies = diagnostics["buffer_copy_requests"]
    if uploaded is not None:
        for name in buffers:
            assert bool(copies.get(name, {}).get("calls", 0)) == uploaded, (label, name, copies)
    report["events"].append(dict(label=label, status=status, diagnostics=diagnostics))
    path.write_text(json.dumps(report, indent=2))
    return status, copies


def interrupt(label, change):
    # The cache report is emitted after construction and before flattening. This
    # observes real worker work, without adding a delay/injection to the engine.
    for attempt in range(3):
        cache_before = len(records("CH_LIGHT_TREE_CACHE"))
        phases_before = len(records("CH_LIGHT_TREE_PHASES"))
        change(attempt)
        generation = session.apply(f.update(), reuse_if_unchanged=True)
        until(lambda: len(records("CH_LIGHT_TREE_CACHE")) > cache_before)
        requested = time.perf_counter()
        session.cancel()
        until(lambda: session.status()["state"] == "cancelled")
        until(lambda: len(records("CH_LIGHT_TREE_PHASES")) > phases_before)
        phases = records("CH_LIGHT_TREE_PHASES")[phases_before:]
        row = dict(
            label=label,
            attempt=attempt,
            generation=generation,
            cancel_seconds=time.perf_counter() - requested,
            phases=phases,
            status=session.status(),
        )
        report["interruptions"].append(row)
        path.write_text(json.dumps(report, indent=2))
        if any(not p["buffers_valid"] and p["nodes"] > 1000000 for p in phases):
            return len(records("CH_LIGHT_TREE_PHASES"))
        # A fast device may finish between observing construction and Stop.
        # Retain the missed attempt and retry a different accepted edit.
        pass_fixture.parm(f.wave, "value").set(0.31 + attempt * 0.01)
        render(label + " retry baseline", value=0.31 + attempt * 0.01)
    raise AssertionError("Did not interrupt the actual million-node lighting update")


try:
    session = ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=4,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined"],
    )
    first, copies = render("Initial buffers are uploaded", True)
    assert first["subdivision_triangles"] >= 1000000, first
    for value in (0.2, 0.3):
        pass_fixture.parm(f.wave, "value").set(value)
        state, _ = render("Independent field edit retains both uploaded arrays", False)
        assert state["device_allocation_bytes"] == first["device_allocation_bytes"], (first, state)
    phase_before = interrupt(
        "Same-size cached flattening is interrupted",
        lambda attempt: pass_fixture.parm(f.obj, "tx").set(0.03 * (attempt + 1)),
    )
    pass_fixture.parm(f.obj, "tx").set(0)
    pass_fixture.parm(f.wave, "value").set(0.4)
    render("Restart after interrupted comparison restores completed uploads")
    # A cancelled Cycles session can first resume the accepted old transaction,
    # then commit the new edit. Check all actual engine updates, rather than
    # requiring the last per-apply diagnostic snapshot to contain the cold copy.
    resumed = records("CH_LIGHT_TREE_PHASES")[phase_before:]
    report["same_size_restart_phases"] = resumed
    assert any(
        p["buffers_valid"] and p["nodes_upload_requested"] and p["emitters_upload_requested"] for p in resumed
    ), resumed
    pass_fixture.parm(f.wave, "value").set(0.5)
    render("Completed restart can retain its arrays again", False)
    interrupt(
        "Resized tree flattening is interrupted", lambda attempt: pass_fixture.parm(grid, "rows").set(182 + attempt)
    )
    pass_fixture.parm(grid, "rows").set(181)
    pass_fixture.parm(f.wave, "value").set(0.6)
    enabled, copies = render("Topology repair restores current data after interrupted resize", True)
    tree_bytes = sum(copies[name]["requested_bytes"] for name in buffers)
    pass_fixture.parm(f.rop, "light_tree").set(0)
    disabled, disabled_copies = render("Disabling the tree immediately retires its large device arrays", False)
    # The non-tree path allocates a different global light distribution and can
    # change device specialization. Check exact ownership, not total-memory
    # subtraction that confuses replacement allocations with retained arrays.
    storage = records("CH_LIGHT_TREE_STORAGE")[-1]
    assert (
        not storage["retained"]
        and storage["nodes"] == storage["emitters"] == 0
        and not storage["nodes_device"]
        and not storage["emitters_device"]
    ), storage
    assert storage["triangles"] == 0 and not storage["triangles_device"], storage
    report["disabled_storage"] = storage
    report["disabled_tracked_byte_reduction"] = enabled["device_allocation_bytes"] - disabled["device_allocation_bytes"]
    report["replacement_distribution_bytes"] = disabled_copies.get("light_distribution", {}).get("requested_bytes", 0)
    pass_fixture.parm(f.rop, "light_tree").set(1)
    render("Reenabling reconstructs retired arrays", True)
    pass_fixture.parm(f.wave, "value").set(0.7)
    render("Reconstructed arrays become reusable", False)
    pass_fixture.parm(f.emission, "strength").set(0)
    render("Removing all emission leaves the geometry visible", False, value=0)
    pass_fixture.parm(f.emission, "strength").set(1)
    render("Restoring emission cannot reuse retired emitter data", True)
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0, closed
    report.update(result="passed", closed=closed, tree_bytes=tree_bytes)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close(wait=True)
    if previous_ranges is None:
        os.environ.pop("CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_TREE_RANGES", None)
    else:
        os.environ["CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_TREE_RANGES"] = previous_ranges
    if previous is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = previous
    path.write_text(json.dumps(report, indent=2))
print(
    json.dumps(dict(result=report["result"], events=len(report["events"]), interruptions=len(report["interruptions"])))
)
