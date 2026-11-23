# SPDX-License-Identifier: Apache-2.0
"""Profile cold, reassigned and partially new frame sets through the normal scene capture."""

import argparse
import json
from pathlib import Path
import threading
from time import perf_counter

import hou
from cycles_houdini import blender_assets, blend_worker, native_scene, instancing


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--object", default="Shape")
parser.add_argument("--out", required=True, type=Path)
args = parser.parse_args()
reference = args.source.resolve(strict=True).as_posix() + "#object=" + args.object
obj = hou.node("/obj").createNode("geo", "frame_cache_points", run_init_scripts=False)
points = obj.createNode("attribwrangle")
points.parm("class").set(0)
points.setDisplayFlag(True)
points.setRenderFlag(True)
other = hou.node("/obj").createNode("geo", "other_frame_cache_points", run_init_scripts=False)
other_points = other.createNode("attribwrangle")
other_points.parm("class").set(0)
other_points.setDisplayFlag(True)
other_points.setRenderFlag(True)
ordinary = hou.node("/obj").createNode("geo", "ordinary_frame_cache_geometry", run_init_scripts=False)
box = ordinary.createNode("box")
box.setDisplayFlag(True)
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam")
rop = hou.node("/out").createNode("cycles_ipr")
rop.setParms(
    dict(
        camera=camera.path(),
        source_mode="objects",
        objectroot="/obj",
        vobject=obj.name() + " " + other.name(),
        lights="",
        use_material_assignments=1,
    )
)

evaluate = blend_worker.evaluate_many
evaluations = []
batches = []
events = []
reference_reads = []
original_references = instancing.referenced_values
original_warmup = blender_assets.prewarm_prototypes
original_prepare = blender_assets.prepare_prototypes


def references(obj_, sop, now):
    assert threading.current_thread() is threading.main_thread()
    reference_reads.append(sop.path())
    events.append(("discover", sop.path()))
    return original_references(obj_, sop, now)


def warmup(specs):
    assert threading.current_thread() is threading.main_thread()
    events.append(("prewarm", len(specs)))
    handle = original_warmup(specs)
    events.append(("scheduled", handle.scheduled))

    class TrackedWarmup:
        scheduled = handle.scheduled

        def close(self):
            events.append(("closed", len(specs)))
            handle.close()

    return TrackedWarmup()


def prepare(specs):
    assert threading.current_thread() is threading.main_thread()
    events.append(("prepare", len(specs)))
    return original_prepare(specs)


def counted(source, executable, requests, *args, **kwargs):
    batches.append([r["frame"] for r in requests])
    evaluations.extend(r["frame"] for r in requests)
    return evaluate(source, executable, requests, *args, **kwargs)


blend_worker.evaluate_many = counted
instancing.referenced_values = references
blender_assets.prewarm_prototypes = warmup
blender_assets.prepare_prototypes = prepare
rows = []
try:
    process_id = None
    for label, indices, misses in (
        ("cold", [0, 15, 20, 25, 30], [0, 15, 20, 25, 30]),
        ("reassign", [30, 15, 0, 25, 20], []),
        ("one_new", [30, 15, 20, 18, 25], [18]),
        ("return", [0, 15, 20, 25, 30], []),
    ):
        for sop, frames in ((points, indices[:3]), (other_points, indices[3:])):
            sop.parm("snippet").set(
                "int frames[]={" + ",".join(map(str, frames)) + "}; "
                "foreach(int i;int f;frames){int p=addpoint(0,set(float(i),0,0));"
                'setpointattrib(0,"instance",p,' + json.dumps(reference) + ');setpointattrib(0,"index",p,float(f));}'
            )
        evaluations.clear()
        batches.clear()
        events.clear()
        reference_reads.clear()
        start = perf_counter()
        update = native_scene.capture(rop)
        seconds = perf_counter() - start
        assert not [d for d in update["source_diagnostics"] if "unavailable" in d["code"]], update["source_diagnostics"]
        assert sorted(evaluations) == misses, (label, evaluations, misses)
        assert len(batches) == bool(misses), (label, batches)
        assert sorted(reference_reads) == sorted([points.path(), other_points.path()]), reference_reads
        if label == "cold":
            discovered = [i for i, event in enumerate(events) if event[0] == "discover"]
            started = next(i for i, event in enumerate(events) if event[0] == "prewarm")
            assert discovered[0] < started < discovered[1], events
        if label == "one_new":
            assert [event[1] for event in events if event[0] == "scheduled"] == [False, True], events
        assert sum(event[0] == "closed" for event in events) == sum(event[0] == "prewarm" for event in events), events
        workers = list(blend_worker._workers.values())
        assert len(workers) == 1 and workers[0].process.poll() is None
        pid = workers[0].process.pid
        assert process_id is None or pid == process_id, (label, pid, process_id)
        process_id = pid
        mesh_count = len(blender_assets._meshes)
        if not misses:
            assert mesh_count == rows[-1]["resident_meshes"]
        rows.append(
            dict(
                edit=label,
                seconds=seconds,
                blender_pid=pid,
                evaluated=list(evaluations),
                batches=list(batches),
                resident_frames=len(blender_assets._bundles),
                resident_meshes=mesh_count,
            )
        )
    events.clear()
    reference_reads.clear()
    evaluations.clear()
    rop.parm("vobject").set(ordinary.name())
    ordinary_update = native_scene.capture(rop)
    assert len(ordinary_update["geometry"]) == 1 and not ordinary_update["source_diagnostics"], ordinary_update
    assert reference_reads == [box.path()], reference_reads
    assert not evaluations and not [event for event in events if event[0] in ("prewarm", "prepare")], events
    # Failed capture work and an interrupted discovery both release every
    # speculative startup handle while preserving the caller's exception.
    rop.parm("vobject").set(obj.name() + " " + other.name())
    events.clear()
    original_requests = native_scene.geometry_requests_for_rop

    def failed_request(*args, **kwargs):
        raise ValueError("injected capture failure")

    native_scene.geometry_requests_for_rop = failed_request
    try:
        try:
            native_scene.capture(rop)
            raise AssertionError("Capture failure was swallowed")
        except ValueError as error:
            assert str(error) == "injected capture failure", error
    finally:
        native_scene.geometry_requests_for_rop = original_requests
    assert sum(event[0] == "closed" for event in events) == sum(event[0] == "prewarm" for event in events) > 0, events
    events.clear()

    def interrupted_references(obj_, sop, now):
        if sop == other_points:
            raise KeyboardInterrupt("injected discovery interruption")
        return references(obj_, sop, now)

    instancing.referenced_values = interrupted_references
    try:
        try:
            with instancing.ExternalPlan([points, other_points], hou.time()):
                raise AssertionError("Discovery interruption was swallowed")
        except KeyboardInterrupt as error:
            assert str(error) == "injected discovery interruption", error
    finally:
        instancing.referenced_values = references
    assert sum(event[0] == "closed" for event in events) == sum(event[0] == "prewarm" for event in events) > 0, events
    roots = [node for node in hou.node("/obj").children() if node.userData("cycles_external_blend")]
    assert not any(n.type().category() == hou.sopNodeTypeCategory() for root in roots for n in root.allSubChildren())
    formats = {p.suffix for p in Path(blender_assets._directory.name).rglob("*") if p.is_file()}
    assert not formats.intersection({".npz", ".bgeo", ".sc"}), formats
finally:
    blend_worker.evaluate_many = evaluate
    instancing.referenced_values = original_references
    blender_assets.prewarm_prototypes = original_warmup
    blender_assets.prepare_prototypes = original_prepare
    blend_worker.shutdown_all()
args.out.parent.mkdir(parents=True, exist_ok=True)
args.out.write_text(json.dumps(dict(result="passed", runs=rows), indent=2))
print(json.dumps(rows))
