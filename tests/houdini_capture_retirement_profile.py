# SPDX-License-Identifier: Apache-2.0
"""Owned background capture retirement and host finalization; excludes rendering/paint."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import json
import time
import traceback

import hou
import cycles_houdini as ch
from stylesheet_storage_fixture import Fixture


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--counts", nargs="+", type=int, default=[1000, 10000, 100000])
args = parser.parse_args()
f = Fixture("CPU", heavy=False)
f.enable_material_motion()
f.prototype.setParms(dict(rows=721, cols=721))
f.prototype.parmTuple("size").set((0.002, 0.001))
f.targets.parmTuple("size").set((4, 2.25))
report = dict(
    result="running", scope=__doc__, requested_device=args.device, capture_device="CPU", records=[], closes=[]
)
path = f.dest / "capture-retirement-report.json"
handle = job = reference = recovered = None


def prepare(data, executor, stage="convert"):
    global job
    if "geometry" in data:
        job = ch._native.acquire_apply(handle, data, hou.time())
        for _ in range(1024):
            executor.submit(ch._native.prewarm_apply, job).result(timeout=30)
            if stage == "scan":
                return
            if not ch._native.expand_apply(job):
                break
        else:
            raise AssertionError("Packed acquisition made no progress")
        for _ in range(1024):
            if ch._native.prepare_apply(handle, data, hou.time(), False, job):
                break
        else:
            raise AssertionError("Host preparation made no progress")
        if stage == "prepare":
            return
    else:
        job = ch._native.prepare_apply(handle, data, hou.time(), False)
    executor.submit(ch._native.convert_apply, job).result(timeout=30)


def accept(data, executor):
    global job
    prepare(data, executor)
    result = ch._native.finish_apply(job)
    job = None
    return result


try:
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="Owned capture retirement profile") as executor:
        for count in args.counts:
            assert count >= 1000 and count % 100 == 0, count
            f.targets.setParms(dict(rows=count // 100, cols=100))
            f.obj.parm("tx").set(0)
            handle = ch._native.create("CPU", 160, 90, 1, True, False, False, False, None, True)
            reference = accept(f.update(), executor)
            info = ch._native.snapshot_info(reference)
            assert info["objects"] == count and info["geometry_resources"] == 1 and info["mesh_triangles"] == 1036800, (
                info
            )
            for stage in ("scan", "prepare", "convert"):
                f.obj.parm("tx").set(f.obj.evalParm("tx") + 0.01)
                prepare(f.update(), executor, stage)
                # Future completion precedes work-item disposal. This barrier
                # ensures the host holds the last job capsule reference here.
                executor.submit(lambda: None).result(timeout=30)
                try:
                    ch._native.retire_capture(handle)
                except RuntimeError as error:
                    assert "scene transaction" in str(error), str(error)
                else:
                    raise AssertionError("A pending transaction was retired")
                started = time.perf_counter()
                future = executor.submit(ch._native.discard_apply, job)
                submission = time.perf_counter() - started
                cleanup = future.result(timeout=30)
                assert not cleanup["on_host_thread"], cleanup
                executor.submit(lambda: None).result(timeout=30)
                started = time.perf_counter()
                job = None
                release = time.perf_counter() - started
                gc.collect()
                recovered = accept(dict(camera_path=f.camera.path()), executor)
                info = ch._native.snapshot_info(recovered, reference)
                assert info["objects"] == count and info["geometry_resources"] == 1, info
                assert not any(
                    info["difference"][key]
                    for key in (
                        "mesh_upserts",
                        "object_upserts",
                        "object_poses",
                        "object_deletions",
                        "geometry_deletions",
                    )
                ), info
                row = dict(
                    instances=count,
                    stage=stage,
                    discard_submission_seconds=submission,
                    background_cleanup=cleanup,
                    last_job_release_seconds=release,
                    host_cleanup_seconds=submission + release,
                    lifecycle=ch._native.native_stats(handle)["scene_lifecycle"],
                )
                report["records"].append(row)
                path.write_text(json.dumps(report, indent=2))
                print(json.dumps(row), flush=True)
            reference = recovered = None
            executor.submit(lambda: None).result(timeout=30)
            gc.collect()
            cleanup = executor.submit(ch._native.retire_capture, handle).result(timeout=30)
            assert not cleanup["on_host_thread"] and cleanup["snapshot_objects"] == count, cleanup
            for operation in (
                lambda: ch._native.prepare_apply(handle, {}, hou.time(), False),
                lambda: ch._native.fixture(handle),
                lambda: ch._native.retire_capture(handle),
            ):
                try:
                    operation()
                except RuntimeError as error:
                    assert "retired" in str(error), str(error)
                else:
                    raise AssertionError("A retired capture handle was reused")
            started = time.perf_counter()
            ch._native.close(handle)
            close = time.perf_counter() - started
            stats = ch._native.native_stats(handle)
            assert stats["scene_lifecycle"]["retired_scenes"] == stats["scene_lifecycle"]["retired_snapshots"] == 0, (
                stats
            )
            row = dict(
                instances=count,
                background_cleanup=cleanup,
                host_close_seconds=close,
                live_geometry_leases=ch._native.geometry_lease_count(),
            )
            assert row["live_geometry_leases"] == 0, row
            report["closes"].append(row)
            handle = None
            print(json.dumps(row), flush=True)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if job is not None:
        ch._native.discard_apply(job)
    if handle is not None:
        ch._native.close(handle)
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], records=len(report["records"]), closes=len(report["closes"]))))
