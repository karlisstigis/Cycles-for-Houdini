# SPDX-License-Identifier: Apache-2.0
"""Exercise mesh-light byte/entry limits, oversized sources and complete retirement."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import sys
import traceback

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage1/light-tree-cache"
destination.mkdir(parents=True, exist_ok=True)
path = destination / "budget-report.json"
report = dict(result="running", scope=__doc__, device=device, checks=[])
original_profile = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
original_disabled = os.environ.pop("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE", None)
original_budget = os.environ.pop("CYCLES_HOUDINI_LIGHT_TREE_CACHE_BUDGET_BYTES", None)
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
obj = hou.node("/obj").createNode("geo", "cache_budget_sources", run_init_scripts=False)
camera = hou.node("/obj").createNode("cam", "cache_budget_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))


def grid(name, rows, size=1.0):
    node = obj.createNode("grid", name)
    node.setParms(dict(type=0, orient=0, rows=rows, cols=rows))
    node.parmTuple("size").set((size, size))
    return node


heavy = [grid("million_triangle_mesh_" + str(n), 721, 1 + n * 0.001) for n in range(2)]
oversized = grid("over_budget_mesh", 1019)
small = [grid("unique_small_mesh_" + str(n), 2, 0.4 + n * 0.0001) for n in range(1025)]
session = ch.Session(
    device=device,
    width=32,
    height=24,
    samples=1,
    interactive=False,
    threads=4,
    execution="worker",
    collect_diagnostics=True,
    use_texture_cache=False,
)
worker_log = None


def records():
    return [
        json.loads(line.split("CH_LIGHT_TREE_CACHE ", 1)[1])
        for line in Path(worker_log).read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith("CH_LIGHT_TREE_CACHE ")
    ]


def render(name, sources):
    global worker_log
    requests = []
    for n, source in enumerate(sources):
        request = ch.geometry_request(obj, source, material="emitter", material_mode="uniform")
        request["object_key"] += ":" + str(n)
        requests.append(request)
    frame = session.wait(
        session.apply(
            dict(
                geometry=requests,
                camera_path=camera.path(),
                background=(0, 0, 0),
                materials=[dict(key="emitter", kind="emission", color=(0.3, 0.8, 0.1))],
            )
        )
    )
    worker_log = session.diagnostics()["controller"]["worker_log"]
    status = session.status()
    assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
    assert status["geometry_resources"] == len(sources), status
    rgba = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(-1, 4)
    assert np.isfinite(rgba).all()
    if sources:
        assert rgba[:, :3].max() > 0.5 and np.count_nonzero(rgba[:, 1] > 0.5) > 8, rgba.max(axis=0)
    record = [r for r in records() if r.get("complete")][-1]
    assert (
        record["enabled"]
        and record["bytes"] + record["scratch_reserve_bytes"] <= record["budget_bytes"]
        and record["entries"] <= 1024
    ), record
    report["checks"].append(dict(name=name, cache=record, status=status))
    path.write_text(json.dumps(report, indent=2))
    return record


try:
    calibration = render("Two dense resources use exact blocks under the production budget", heavy)
    assert calibration["entries"] == calibration["compact_entries"] == 2 and calibration["evictions"] == 0, calibration
    # Derive a smaller diagnostic budget from actual retained capacity so this
    # contract keeps exercising byte eviction when the record format improves.
    budget = (
        calibration["largest_entry_bytes"]
        + calibration["smallest_entry_bytes"] // 2
        + 2 * calibration["scratch_reserve_bytes"]
    )
    assert calibration["largest_entry_bytes"] + calibration["scratch_reserve_bytes"] < budget < calibration["bytes"], (
        calibration
    )
    session.close(wait=True)
    report["calibration_closed"] = session.status()
    assert (
        report["calibration_closed"]["owned_geometry_bytes"]
        == report["calibration_closed"]["device_allocation_bytes"]
        == 0
    )
    os.environ["CYCLES_HOUDINI_LIGHT_TREE_CACHE_BUDGET_BYTES"] = str(budget)
    session = ch.Session(
        device=device,
        width=32,
        height=24,
        samples=1,
        interactive=False,
        threads=4,
        execution="worker",
        collect_diagnostics=True,
        use_texture_cache=False,
    )
    worker_log = None
    first = render("Two compact dense resources exceed the diagnostic byte budget and evict the oldest entry", heavy)
    assert first["misses"] == 2 and first["evictions"] >= 1 and first["entries"] == 1, first
    obj.parm("tx").set(0.01)
    second = render("A live mesh still renders after byte-budget eviction; retained data stays bounded", heavy)
    assert second["hits"] == second["misses"] == second["admission_skips"] == 1, second
    assert second["evictions"] == 0, second
    assert second["entries"] == 1, second
    # A current view pins its entry. A competing over-budget candidate renders
    # normally without evicting that view or accumulating another allocation.
    for repetition in range(4):
        obj.parm("tx").set(0.02 + repetition * 0.01)
        repeated = render("Repeated over-budget admission preserves the borrowed entry and bounded storage", heavy)
        expected = first
        assert repeated["entries"] == 1 and repeated["bytes"] == expected["bytes"], (expected, repeated)
        assert repeated["admission_skips"] == 1 and repeated["evictions"] == 0, repeated
    large = render("An individual over-budget mesh uses normal construction and remains visible", [oversized])
    assert large["misses"] == 1 and large["entries"] == large["bytes"] == 0, large
    limited = render("1025 distinct small sources exercise the independent entry limit", small)
    assert limited["misses"] == 1025 and limited["evictions"] == 1 and limited["entries"] == 1024, limited
    obj.parm("tx").set(0.08)
    moved = render("Transforming entry-limited sources retains compatible data and bounded storage", small)
    assert moved["hits"] == 1024 and moved["misses"] == moved["admission_skips"] == 1, moved
    assert moved["evictions"] == 0, moved
    assert moved["bytes"] == limited["bytes"], (limited, moved)
    remaining = render("Removing most sources prunes every no-longer-live cache entry", small[:32])
    assert remaining["entries"] == 32 and remaining["bytes"] < moved["bytes"], remaining
    empty = render("Removing all sources releases retained storage", [])
    assert empty["entries"] == empty["bytes"] == 0, empty
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    unwinding = sys.exc_info()[0] is not None
    try:
        session.close(wait=True)
        report["closed"] = session.status()
        if worker_log is not None:
            report["cache_records"] = records()
            assert report["cache_records"][-1].get("retired") and report["cache_records"][-1]["bytes"] == 0
        assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    except BaseException:
        report.update(result="failed", cleanup_error=traceback.format_exc())
        if not unwinding:
            report["error"] = report["cleanup_error"]
            raise
    finally:
        path.write_text(json.dumps(report, indent=2))
        if original_profile is None:
            os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
        else:
            os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = original_profile
        if original_disabled is not None:
            os.environ["CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE"] = original_disabled
        if original_budget is None:
            os.environ.pop("CYCLES_HOUDINI_LIGHT_TREE_CACHE_BUDGET_BYTES", None)
        else:
            os.environ["CYCLES_HOUDINI_LIGHT_TREE_CACHE_BUDGET_BYTES"] = original_budget
print(path)
