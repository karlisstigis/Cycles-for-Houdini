# SPDX-License-Identifier: Apache-2.0
"""Warm 960x540 updates beside twelve retained million-triangle mesh lights.

Times include native capture, submission and complete worker-frame readback;
GUI paint and physical input are measured separately. Cache contracts are strict;
timing distributions are observations, not universal acceptance guarantees.
"""

from support.paths import artifacts_root
import argparse, json, math, os, time, traceback
from pathlib import Path
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from mesh_island_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/performance/retained-emitters"
dest.mkdir(parents=True, exist_ok=True)
previous = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
f = Fixture(device)
f.width, f.height = 960, 540
f.rop.setParms(dict(resx=f.width, resy=f.height))
f.add_heavy()
report = dict(result="running", device=device, scope=__doc__, events=[])
session = None
log_path = None
baseline = None


def records():
    return (
        [
            json.loads(line.split(" ", 1)[1])
            for line in Path(log_path).read_text(errors="replace").splitlines()
            if line.startswith("CH_LIGHT_TREE_CACHE ")
        ]
        if log_path
        else []
    )


def render(label, kind, strength=1.0):
    global log_path, baseline
    previous_count = len(records())
    start = time.perf_counter()
    capture = native_scene.capture(f.rop)
    captured = time.perf_counter()
    generation = session.apply(capture, reuse_if_unchanged=True)
    submitted = time.perf_counter()
    frame = session.wait(generation)
    finished = time.perf_counter()
    status = session.status()
    diag = session.diagnostics()
    log_path = diag["controller"]["worker_log"] or log_path
    values = f.inspect(frame, status, expected=None if baseline is None else baseline * strength)
    if baseline is None:
        baseline = np.array(values)
    assert f.heavy.cookCount() == 1 and status["packed_instances"] == 12, status
    cache = [r for r in records()[previous_count:] if r.get("complete")]
    if kind != "initial" and cache:
        # Only the edited small mesh may need a new local emitter hierarchy.
        assert all(r["hits"] >= 1 and r["misses"] <= 1 for r in cache), cache
    if report["events"]:
        old = report["events"][-1]["status"]
        assert status["renderer_pid"] == old["renderer_pid"]
        if kind in ("camera", "transform", "material"):
            assert status["geometry_conversions"] == old["geometry_conversions"], status
        assert status["owned_geometry_bytes"] == report["events"][0]["status"]["owned_geometry_bytes"], status
    report["events"].append(
        dict(
            label=label,
            kind=kind,
            capture_seconds=captured - start,
            submit_seconds=submitted - captured,
            complete_frame_seconds=finished - start,
            first_image_worker_seconds=status["first_image_seconds"],
            status=status,
            diagnostics=diag,
            cache=cache,
            heavy_cooks=1,
        )
    )
    (dest / "report.json").write_text(json.dumps(report, indent=2))


try:
    session = ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        execution="worker",
        threads=2,
        collect_diagnostics=True,
        use_texture_cache=False,
        passes=["combined", "position"],
    )
    render("Initial retained emitters", "initial")
    strength = 1.0
    for n in range(5):
        f.camera.parm("tx").set(0.01 * (n + 1))
        render("Camera " + str(n), "camera", strength)
        f.obj.parm("tx").set(0.01 * (n + 1))
        render("Object " + str(n), "transform", strength)
        f.set_mesh(deform=0.001 * (n + 1))
        render("Small mesh " + str(n), "geometry", strength)
        strength = 0.5 + 0.1 * n
        f.emission.parm("strength").set(strength)
        render("Material " + str(n), "material", strength)
    distributions = {}
    for kind in ("camera", "transform", "geometry", "material"):
        values = sorted(e["complete_frame_seconds"] for e in report["events"] if e["kind"] == kind)
        distributions[kind] = dict(
            count=len(values),
            median_seconds=values[len(values) // 2],
            p95_seconds=values[math.ceil(0.95 * len(values)) - 1],
            maximum_seconds=values[-1],
        )
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0, closed
    retired = records()
    assert retired[-1].get("retired") and retired[-1]["bytes"] == 0, retired[-1]
    report.update(
        result="passed",
        warm_complete_frame_distributions=distributions,
        closed=closed,
        cache_retirement=retired[-1],
        heavy_triangles=1048576,
        shared_copies=12,
    )
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
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        dict(
            result=report["result"],
            events=len(report["events"]),
            distributions=report.get("warm_complete_frame_distributions"),
        )
    )
)
