# SPDX-License-Identifier: Apache-2.0
"""Phase profile for packed velocity; excludes rendering, device work and paint."""

import argparse, json, time, traceback
from pathlib import Path
import cycles_houdini as ch
from packed_velocity_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--counts", nargs="+", type=int, default=[1000, 10000, 100000])
args = parser.parse_args()
f = Fixture(args.device)
f.grid.parmTuple("size").set((0.01, 0.01))
path = f.dest / "capture-profile.json"
report = dict(result="running", scope=__doc__, records=[])
try:
    for count in args.counts:
        assert count >= 200 and count % 100 == 0
        f.targets.setParms(dict(rows=count // 100, cols=100))
        f.targets.parmTuple("size").set((4, 2.25))
        handle = ch._native.create("CPU", 160, 90, 1, True, False, False, False, None, True)
        previous = None
        try:
            for number, workload in enumerate(("cold", "warm", "velocity", "camera", "object", "prototype")):
                if workload == "velocity":
                    f.velocity.parm("test_velocity").set(0.5)
                if workload == "camera":
                    f.camera.parm("tx").set(0.05)
                if workload == "object":
                    f.obj.parm("tx").set(0.05)
                if workload == "prototype":
                    f.grid.parmTuple("size").set((0.012, 0.01))
                update = dict(camera_path=f.camera.path()) if workload == "camera" else f.update(sop=f.copies)
                row = dict(instances=count, workload=workload)
                started = time.perf_counter()
                if "geometry" in update:
                    at = time.perf_counter()
                    job = ch._native.acquire_apply(handle, update, 0.0)
                    row["acquire"] = time.perf_counter() - at
                    scan = expand = 0.0
                    while True:
                        at = time.perf_counter()
                        ch._native.prewarm_apply(job)
                        scan += time.perf_counter() - at
                        at = time.perf_counter()
                        more = ch._native.expand_apply(job)
                        expand += time.perf_counter() - at
                        if not more:
                            break
                    at = time.perf_counter()
                    while not ch._native.prepare_apply(handle, update, 0.0, False, job):
                        pass
                    row.update(scan=scan, expand=expand, prepare=time.perf_counter() - at)
                else:
                    at = time.perf_counter()
                    job = ch._native.prepare_apply(handle, update, 0.0, False)
                    row["prepare"] = time.perf_counter() - at
                at = time.perf_counter()
                ch._native.convert_apply(job)
                row["convert"] = time.perf_counter() - at
                at = time.perf_counter()
                snapshot = ch._native.finish_apply(job)
                row["finish"] = time.perf_counter() - at
                row["capture_total"] = time.perf_counter() - started
                row["inventory"] = ch._native.snapshot_info(snapshot)
                assert row["inventory"]["objects"] == count and row["inventory"]["geometry_resources"] == 1, row
                at = time.perf_counter()
                packet = ch._native.encode_snapshot(snapshot, previous)
                row["encode"] = time.perf_counter() - at
                row["wire_bytes"] = len(packet)
                row["stats"] = ch._native.native_stats(handle)
                assert row["stats"]["motion_cooks"] == 0, row
                previous = snapshot
                report["records"].append(row)
                path.write_text(json.dumps(report, indent=2))
                print(json.dumps({k: v for k, v in row.items() if k not in ("stats", "inventory")}), flush=True)
        finally:
            ch._native.close(handle)
        f.velocity.parm("test_velocity").set(1)
        f.camera.parm("tx").set(0)
        f.obj.parm("tx").set(0)
        f.grid.parmTuple("size").set((0.01, 0.01))
    report["result"] = "passed"  # Inventory/no-cook contracts only; timings have no pass budget.
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2))
