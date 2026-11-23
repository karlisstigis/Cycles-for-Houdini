# SPDX-License-Identifier: Apache-2.0
"""Separate dense lighting construction, flattening and transfer observations."""

import argparse, json, os, time, traceback
from pathlib import Path
import cycles_houdini as ch
from cycles_houdini import device_selection
from stylesheet_storage_fixture import Fixture
from support.devices import render_device_evidence

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device[0] if isinstance(device, list) else device)
if isinstance(device, list):
    device_selection.set_devices(f.rop, device)
dest = f.dest.parent / "lighting-transfer"
dest.mkdir(exist_ok=True)
path = dest / "profile-report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[])
previous = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
counts = {}


def new_records(log, marker):
    rows = [
        json.loads(line[len(marker) :])
        for line in Path(log).read_text(errors="replace").splitlines()
        if line.startswith(marker)
    ]
    result = rows[counts.get(marker, 0) :]
    counts[marker] = len(rows)
    return result


steps = [
    ("Uniform cold", lambda: f.styles("uniform"), 0),
    ("Four palettes cold", lambda: f.styles(), 0),
    ("Emission edit cold", lambda: f.styles(offset=0.03), 0.03),
    ("Camera", lambda: f.camera.parm("tx").set(0.05), 0.03),
]
for index in range(6):
    steps.append(("Warm object " + str(index), lambda value=index: f.obj.parm("tx").set(0.03 * (value + 1)), 0.03))
steps += [("Restore emission", lambda: f.styles(), 0), ("Styles removed", lambda: f.styles("uniform"), 0)]
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:
        for label, edit, offset in steps:
            edit()
            start = time.perf_counter()
            frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True), timeout=240)
            elapsed = time.perf_counter() - start
            status = session.status()
            render_device_evidence(status, device)
            colors = f.check(frame, offset)
            assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
            assert status["object_resources"] == 12 and status["mesh_variant_resources"] == 1, status
            diagnostics = session.diagnostics()
            log = diagnostics["controller"]["worker_log"]
            assert log
            phases = new_records(log, "CH_LIGHT_TREE_PHASES ")
            cache = new_records(log, "CH_LIGHT_TREE_CACHE ")
            build_phases = new_records(log, "CH_LIGHT_TREE_BUILD_PHASES ")
            build_detail = new_records(log, "CH_LIGHT_TREE_BUILD_DETAIL ")
            expected_skip = os.environ.get("CYCLES_HOUDINI_DISABLE_EMPTY_LIGHT_TREE_SPLIT_SKIP") != "1"
            assert all(b["duplicate_split_skip"] == expected_skip for b in build_phases + build_detail), (
                expected_skip,
                build_phases,
                build_detail,
            )
            if os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE_DETAIL") == "1" and "cold" in label:
                assert any(d["complete"] and d["stages"]["decision"]["samples"] > 0 for d in build_detail), build_detail
            complete = [row for row in phases if row["complete"]]
            if label == "Camera":
                assert not phases, phases
            if label.startswith("Warm object"):
                assert (
                    complete
                    and cache
                    and all(
                        row["hits"] and row["misses"] == row["admission_skips"] == 0 for row in cache if row["complete"]
                    )
                ), cache
                assert status["geometry_conversions"] == report["checks"][-1]["status"]["geometry_conversions"], status
            row = dict(
                label=label,
                complete_image_seconds=elapsed,
                prototype_cooks=f.prototype.cookCount(),
                colors=colors,
                status=status,
                phases=phases,
                cache=cache,
                build_phases=build_phases,
                build_detail=build_detail,
                diagnostics=diagnostics,
            )
            report["checks"].append(row)
            path.write_text(json.dumps(report, indent=2))
        assert len({row["prototype_cooks"] for row in report["checks"]}) == 1
        if os.environ.get("CYCLES_HOUDINI_REQUIRE_RETAINED_LIGHT_TREE_RANGES") == "1":
            assert any(
                phase.get("retained_cached_nodes", 0) > 0
                for row in report["checks"]
                if row["label"].startswith("Warm object")
                for phase in row["phases"]
                if phase["complete"]
            ), report["checks"]
        if os.environ.get("CYCLES_HOUDINI_REQUIRE_LIGHT_TREE_RANGE_UPLOAD") == "1":
            for row in report["checks"]:
                if not row["label"].startswith("Warm object"):
                    continue
                phases = [p for p in row["phases"] if p["complete"]]
                assert phases and all(
                    p["nodes_partial_upload"]
                    and 0 < p["nodes_upload_bytes"] < 1024 * 1024
                    and 0 < p["nodes_upload_spans"] <= 64
                    for p in phases
                ), (row["label"], phases)
                copies = row["diagnostics"]["buffer_copy_requests"]["light_tree_nodes"]
                assert copies["requested_bytes"] == sum(p["nodes_upload_bytes"] for p in phases), (copies, phases)
        if os.environ.get("CYCLES_HOUDINI_REQUIRE_LIGHT_TREE_EMITTER_VIEWS") == "1":
            for row in report["checks"]:
                if not row["label"].startswith("Warm object"):
                    continue
                phases = [p for p in row["phases"] if p["complete"]]
                assert phases and all(
                    p["dense_source_emitters"] == 12
                    and p["cached_source_emitters"] == 4194304
                    and p["source_spans"] == 4
                    for p in phases
                ), (row["label"], phases)
                assert all(
                    c["materialized_triangles"] == 0 and c["borrowed_triangles"] == 4194304
                    for c in row["cache"]
                    if c["complete"]
                ), row["cache"]
    report.update(result="passed", closed=session.status())
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    if previous is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = previous
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
