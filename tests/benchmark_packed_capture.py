# SPDX-License-Identifier: Apache-2.0
"""Diagnostic phase profile; excludes a render session, GPU work and GUI paint."""

from native_test_runtime import build_directory, build_manifest
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import hou
import cycles_houdini as ch

p = argparse.ArgumentParser()
p.add_argument("--counts", nargs="+", type=int, default=[1000, 10000, 100000])
p.add_argument("--rows", type=int, default=129)
p.add_argument("--repetitions", type=int, default=2)
p.add_argument("--destination", type=Path, required=True)
args = p.parse_args()
args.destination.mkdir(parents=True, exist_ok=False)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
manifest = build_manifest()
(args.destination / "build-manifest.json").write_bytes(manifest.read_bytes())
manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
frozen = json.loads(manifest.read_text(encoding="utf-8-sig"))


def verify_inputs():
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_hash, "Build manifest changed during profiling"
    for item in frozen["integration_source"]:
        assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"].lower(), item["path"]
    for item in frozen["binary_artifacts"]:
        assert hashlib.sha256((build_directory() / item["path"]).read_bytes()).hexdigest() == item["sha256"].lower(), (
            item["path"]
        )


verify_inputs()
report = dict(
    result="running",
    scope=__doc__,
    counts=args.counts,
    rows=args.rows,
    records=[],
    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    build_manifest_sha256=manifest_hash,
)
for count in args.counts:
    assert count % 100 == 0 and count >= 200
    obj = hou.node("/obj").createNode("geo", "packed_phase_profile", run_init_scripts=False)
    prototype = obj.createNode("grid", "shared_prototype")
    prototype.setParms(dict(rows=args.rows, cols=args.rows, orient=0))
    prototype.parmTuple("size").set((0.01, 0.01))
    deform = obj.createNode("xform", "prototype_edit")
    deform.setInput(0, prototype)
    targets = obj.createNode("grid", "instance_points")
    targets.setParms(dict(rows=count // 100, cols=100, orient=0))
    targets.parmTuple("size").set((4, 2.25))
    move = obj.createNode("xform", "instance_edit")
    move.setInput(0, targets)
    copies = obj.createNode("copytopoints")
    copies.setInput(0, deform)
    copies.setInput(1, move)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    camera = hou.node("/obj").createNode("cam", "packed_profile_camera")
    camera.setParms(dict(tz=5, projection=1, orthowidth=4.5))
    h = ch._native.create("CPU", 960, 540, 1, True, False, True, False, None, True)
    previous = None
    try:
        for workload in ["cold"] + [
            w for w in ("camera", "object", "packed", "prototype") for _ in range(args.repetitions)
        ]:
            trial = dict(instances=count, workload=workload)
            n = len(report["records"])
            if workload == "camera":
                camera.parm("tx").set(0.002 * n)
            elif workload == "object":
                obj.parm("tx").set(0.002 * n)
            elif workload == "packed":
                move.parm("tx").set(0.002 * n)
            elif workload == "prototype":
                deform.parm("rx").set(2 * n)
            update = dict(camera_path=camera.path())
            if workload != "camera":
                update.update(
                    geometry=[ch.geometry_request(obj, copies, material="surface", material_mode="uniform")],
                    materials=[dict(key="surface", kind="emission", color=(0.2, 0.6, 0.3), strength=1)],
                )
            t = time.perf_counter()
            if "geometry" in update:
                job = ch._native.acquire_apply(h, update, hou.time())
                trial["acquire"] = time.perf_counter() - t
                scan = expand = 0.0
                steps = 0
                while True:
                    a = time.perf_counter()
                    ch._native.prewarm_apply(job)
                    scan += time.perf_counter() - a
                    a = time.perf_counter()
                    more = ch._native.expand_apply(job)
                    expand += time.perf_counter() - a
                    steps += 1
                    if not more:
                        break
                a = time.perf_counter()
                while not ch._native.prepare_apply(h, update, hou.time(), False, job):
                    pass
                trial.update(scan=scan, expand=expand, frontiers=steps, prepare=time.perf_counter() - a)
            else:
                a = time.perf_counter()
                job = ch._native.prepare_apply(h, update, hou.time(), False)
                trial["prepare"] = time.perf_counter() - a
            a = time.perf_counter()
            ch._native.convert_apply(job)
            trial["convert"] = time.perf_counter() - a
            a = time.perf_counter()
            snapshot = ch._native.finish_apply(job)
            trial["finish"] = time.perf_counter() - a
            trial["total_capture"] = time.perf_counter() - t
            trial["inventory"] = ch._native.snapshot_info(snapshot)
            assert trial["inventory"]["objects"] == count and trial["inventory"]["geometry_resources"] == 1, trial
            a = time.perf_counter()
            packet = ch._native.encode_snapshot(snapshot, previous)
            trial["encode"] = time.perf_counter() - a
            trial["wire_bytes"] = len(packet)
            trial["extractions"] = ch._native.status(h)["extractions"]
            trial["process_memory"] = ch.process_memory()
            previous = snapshot
            report["records"].append(trial)
            (args.destination / "report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps({k: v for k, v in trial.items() if k not in ("process_memory", "inventory")}), flush=True)
    finally:
        ch._native.close(h)
        obj.destroy()
        camera.destroy()
verify_inputs()
report["result"] = "measured"
report["frozen_inputs_verified_before_and_after"] = True
(args.destination / "report.json").write_text(json.dumps(report, indent=2))
