# SPDX-License-Identifier: Apache-2.0
"""Measure dense packed surface variants; capture, encoded transport and device storage separately."""

import argparse
import json
import os
from pathlib import Path
import time
import traceback
import hou
import cycles_houdini as ch
from cycles_houdini import _native
from stylesheet_storage_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, scope=__doc__, checks=[])
previous = None
original_profile = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
log_path = None
cache_count = 0
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
        for label, change, offset in (
            ("Uniform source", lambda: f.styles("uniform"), 0),
            ("Four surface styles", lambda: f.styles(), 0),
            ("Surface value edit", lambda: f.styles(offset=0.03), 0.03),
            ("Camera move", lambda: f.camera.parm("tx").set(0.05), 0.03),
            ("Object move", lambda: f.obj.parm("tx").set(0.05), 0.03),
            ("Styles removed", lambda: f.styles("uniform"), 0),
        ):
            change()
            began = time.perf_counter()
            frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True), timeout=240)
            elapsed = time.perf_counter() - began
            status = session.status()
            assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
            assert status["object_resources"] == 12, status
            assert status["mesh_variant_resources"] == 1, status
            colors = f.check(frame, offset)
            diagnostics = session.diagnostics()
            log_path = diagnostics["controller"]["worker_log"] or log_path
            records = [
                json.loads(line.split(" ", 1)[1])
                for line in Path(log_path).read_text().splitlines()
                if line.startswith("CH_LIGHT_TREE_CACHE ")
            ]
            cache = [row for row in records[cache_count:] if row.get("complete")]
            cache_count = len(records)
            assert all(row["bytes"] + row["scratch_reserve_bytes"] <= row["budget_bytes"] for row in cache), cache
            if label == "Object move":
                assert cache and all(
                    row["hits"] > 0 and row["misses"] == row["admission_skips"] == 0 for row in cache
                ), cache
            if label == "Camera move":
                assert not cache, cache
            snapshot = session._controller.acknowledged
            inventory = _native.snapshot_info(snapshot, previous) if previous else _native.snapshot_info(snapshot)
            began = time.perf_counter()
            packet = _native.encode_snapshot(snapshot, previous) if previous else _native.encode_snapshot(snapshot)
            encoded = time.perf_counter() - began
            event = dict(
                label=label,
                colors=colors,
                status=status,
                inventory=inventory,
                complete_render_seconds=elapsed,
                encoded_delta_bytes=len(packet),
                encode_seconds=encoded,
                prototype_cooks=f.prototype.cookCount(),
                process_memory=ch.process_memory(),
                diagnostics=diagnostics,
                cache=cache,
            )
            report["checks"].append(event)
            previous = snapshot
            (f.dest / "baseline.json").write_text(json.dumps(report, indent=2))
        assert len({e["prototype_cooks"] for e in report["checks"]}) == 1
    report.update(result="passed", closed=session.status())
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    (f.dest / "baseline.json").write_text(json.dumps(report, indent=2))
    if original_profile is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = original_profile
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
