# SPDX-License-Identifier: Apache-2.0
"""Packed material-motion capture phases; excludes rendering, device work and GUI paint."""

import argparse
import json
import os
import time
import traceback
from pathlib import Path

import hou
import cycles_houdini as ch
from stylesheet_storage_fixture import Fixture


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--counts", nargs="+", type=int, default=[1000, 10000, 100000])
parser.add_argument("--prototype-rows", type=int, default=721)
args = parser.parse_args()
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
f = Fixture("CPU")
f.width, f.height = 160, 90
f.rop.setParms(dict(resx=f.width, resy=f.height))
f.prototype.setParms(dict(rows=args.prototype_rows, cols=args.prototype_rows))
f.prototype.parmTuple("size").set((0.002, 0.001))
f.targets.parmTuple("size").set((4, 2.25))
f.camera.setParms(dict(tz=5, orthowidth=4.5))
f.enable_material_motion()
destination = f.dest / "packed-material-motion-acquisition"
destination.mkdir(parents=True, exist_ok=True)
path = destination / "report.json"
report = dict(result="running", scope=__doc__, requested_device=args.device, capture_device="CPU", records=[])
height = None

try:
    assert args.prototype_rows >= 2
    for count in args.counts:
        assert count >= 200 and count % 100 == 0
        f.targets.setParms(dict(rows=count // 100, cols=100))
        f.obj.parm("tx").set(0)
        f.camera.parm("tx").set(0)
        f.obj.parm("shop_materialstylesheet").set("")
        if height is not None:
            f.material.node("surface_output").setInput(1, None)
            height.destroy()
            height = None
        f.material.parm("cycles_displacement_method").set("bump")
        handle = ch._native.create("CPU", f.width, f.height, 1, True, False, False, False, None, True)
        previous = None
        job = None
        try:
            for workload in (
                "cold",
                "surface",
                "surface_edit",
                "displacement",
                "displacement_edit",
                "object",
                "camera",
                "retire",
            ):
                if workload in ("surface", "surface_edit"):
                    tint = [0.2, 0.4, 0.6 if workload == "surface" else 0.65]
                    f.obj.parm("shop_materialstylesheet").set(
                        json.dumps(dict(styles=[dict(overrides=dict(materialParameters=dict(tint=tint)))]))
                    )
                elif workload == "displacement":
                    height = f.material.createNode("cycles::displacement::1", "packed_profile_height")
                    height.setParms(dict(height=0.002, midlevel=0, scale=1, space="object"))
                    f.material.node("surface_output").setNamedInput("displacement", height, "displacement")
                    f.material.parm("cycles_displacement_method").set("true")
                elif workload == "displacement_edit":
                    height.parm("height").set(0.003)
                elif workload == "object":
                    f.obj.parm("tx").set(0.01)
                elif workload == "camera":
                    f.camera.parm("tx").set(0.01)
                update = (
                    dict(camera_path=f.camera.path())
                    if workload == "camera"
                    else dict(geometry=[])
                    if workload == "retire"
                    else f.update()
                )
                row = dict(instances=count, workload=workload)
                began = time.perf_counter()
                row["host_turns"] = []
                row["expand_turns"] = []
                if "geometry" in update:
                    started = time.perf_counter()
                    job = ch._native.acquire_apply(handle, update, hou.time())
                    row["acquire_seconds"] = time.perf_counter() - started
                    row["scan_seconds"] = row["expand_seconds"] = 0.0
                    while True:
                        started = time.perf_counter()
                        ch._native.prewarm_apply(job)
                        row["scan_seconds"] += time.perf_counter() - started
                        started = time.perf_counter()
                        more = ch._native.expand_apply(job)
                        duration = time.perf_counter() - started
                        row["expand_turns"].append(duration)
                        row["expand_seconds"] += duration
                        if not more:
                            break
                    ready = False
                    while not ready:
                        assert len(row["host_turns"]) < 4096, "Preparation made no bounded progress"
                        started = time.perf_counter()
                        ready = ch._native.prepare_apply(handle, update, hou.time(), False, job)
                        row["host_turns"].append(time.perf_counter() - started)
                else:
                    started = time.perf_counter()
                    job = ch._native.prepare_apply(handle, update, hou.time(), False)
                    row["host_turns"].append(time.perf_counter() - started)
                started = time.perf_counter()
                ch._native.convert_apply(job)
                row["convert_seconds"] = time.perf_counter() - started
                started = time.perf_counter()
                snapshot = ch._native.finish_apply(job)
                job = None
                row["finish_seconds"] = time.perf_counter() - started
                row["capture_seconds"] = time.perf_counter() - began
                info = (
                    ch._native.snapshot_info(snapshot, previous)
                    if previous is not None
                    else ch._native.snapshot_info(snapshot)
                )
                assert info["objects"] == (0 if workload == "retire" else count) and info["geometry_resources"] == (
                    0 if workload == "retire" else 1
                ), (workload, info)
                assert info["mesh_triangles"] == (0 if workload == "retire" else 2 * (args.prototype_rows - 1) ** 2), (
                    workload,
                    info,
                )
                if workload == "camera":
                    assert info["difference"]["mesh_upserts"] == info["difference"]["object_upserts"] == 0, info
                row["inventory"] = info
                row["stats"] = ch._native.native_stats(handle)
                assert not row["stats"]["motion_warnings"] and row["stats"]["material_motion_cooks"] == 0, row["stats"]
                assert f.prototype.cookCount() == 1, f.prototype.cookCount()
                started = time.perf_counter()
                packet = ch._native.encode_snapshot(snapshot, previous)
                row["encode_seconds"] = time.perf_counter() - started
                row["wire_bytes"] = len(packet)
                row["maximum_preparation_turn_seconds"] = max(row["host_turns"])
                row["total_preparation_seconds"] = sum(row["host_turns"])
                # The viewer finishes the final expand call and starts prepare
                # in one host callback. Commit is a separate host callback too.
                tail = (row["expand_turns"][-1] if row["expand_turns"] else 0) + row["host_turns"][0]
                row["maximum_host_turn_seconds"] = max(
                    row["host_turns"] + row["expand_turns"] + [tail, row["finish_seconds"]]
                )
                row["prototype_cooks"] = f.prototype.cookCount()
                previous = snapshot
                report["records"].append(row)
                path.write_text(json.dumps(report, indent=2))
                print(
                    json.dumps(
                        {
                            key: row[key]
                            for key in (
                                "instances",
                                "workload",
                                "maximum_host_turn_seconds",
                                "total_preparation_seconds",
                                "capture_seconds",
                                "convert_seconds",
                                "finish_seconds",
                                "encode_seconds",
                            )
                        }
                    ),
                    flush=True,
                )
        finally:
            if job is not None:
                ch._native.discard_apply(job)
            ch._native.close(handle)
    report["result"] = "passed"  # Inventory, ownership, cook and camera-delta contracts; no timing budget yet.
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], records=len(report["records"]))))
