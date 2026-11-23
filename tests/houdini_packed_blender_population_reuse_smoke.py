# SPDX-License-Identifier: Apache-2.0
"""Capture-only current packed-Blender population reuse and invalidation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import traceback

import hou
import cycles_houdini as ch
from blend_direct_fixture import mesh, prototype


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--count", type=int, default=8)
args = parser.parse_args()
assert args.count >= 2
args.out.mkdir(parents=True, exist_ok=True)
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "packed_population_reuse", run_init_scripts=False)
obj.parmTuple("s").set((-1.2, 0.8, 1.5))
source = obj.createNode("cycles::blender_reference", "external_reference")
source.parm("ref_frame1").deleteAllKeyframes()
source.setParms(dict(file="fixture.blend", ref_target_kind1="collection", ref_target1="Members", ref_frame1=1.0))
reference = "fixture.blend#collection=Members"
points = obj.createNode("attribwrangle", "target_points")
points.parm("class").set(0)
for name, template in (
    ("count", hou.IntParmTemplate("count", "Count", 1, default_value=(args.count,))),
    ("dx", hou.FloatParmTemplate("dx", "Placement", 1)),
    ("tint", hou.FloatParmTemplate("tint", "Tint", 1, default_value=(0.25,))),
):
    points.addSpareParmTuple(template)
points.parm("snippet").set(
    'for(int i=0;i<chi("count");i++){int p=addpoint(0,set(i*.25+chf("dx"),0,0));'
    'setpointattrib(0,"id",p,i);setpointattrib(0,"Cd",p,set(chf("tint"),float(i)/100.0,.8));}'
)
copies = obj.createNode("copytopoints::2.0", "packed_copies")
copies.setInput(0, source)
copies.setInput(1, points)
copies.parm("pack").set(0)
copies.parm("targetattribs").set(1)
copies.parm("applyattribs1").set("id Cd")
copies.setDisplayFlag(True)
copies.setRenderFlag(True)
members = [
    prototype("packed/member:" + str(index), mesh("packed/mesh:" + str(index)), translate=(0, index * 0.3, 0))[
        "instances"
    ][0]
    for index in range(4)
]
surface_color = (0.5, 0.3, 0.2)
pass_index = 0
report = dict(result="running", scope=__doc__, count=args.count, records=[], runtime=str(ch.__file__))
report_path = args.out / "report.json"
handle = ch._native.create("CPU", 8, 8, 1, True, False, False, False, None, True)
oracle = ch._native.create("CPU", 8, 8, 1, True, False, False, False, None, True)
previous = None
unaccepted = None


def primitive_source_frame():
    """The Source Frame every packed primitive carries, as the product's capture reads it.

    Since 917efb6 the SOP's Source Frame is ``ref_frame1``; the legacy ``frame``
    parameter is kept only for old HIP files and no longer drives the primitives.
    The prototypes must be declared at the primitives' frame or none of them match.
    """
    frames = {prim.intrinsicValue("cycles_blender_frame") for prim in copies.geometry().prims()}
    assert frames == {source.evalParm("ref_frame1")}, (frames, source.evalParm("ref_frame1"))
    return float(next(iter(frames)))


def update(material="surface", enabled=False):
    request = ch.geometry_request(obj, copies, material=material, material_mode="uniform")
    request["object_properties"]["pass_index"] = pass_index
    request["packed_blender"] = dict(
        mode="fast",
        instancer_path=obj.path(),
        # This fallback is unused by actual packed primitives. Let it track
        # Houdini time so warm captures exercise effective-request comparison.
        frame=float(hou.frame()),
        fallback=-1,
        prototypes=[
            dict(
                path=reference,
                node_id=0,
                source_reference=reference,
                source_frame=primitive_source_frame(),
                direct_instances=members,
            )
        ],
    )
    return dict(
        geometry=[request],
        materials=[dict(key=material, kind="emission", color=surface_color)],
        motion=dict(enabled=enabled, fps=24, shutter_frames=0.5, offset=0, samples=3),
    )


def capture(target, data, executor, accept=True):
    started = time.perf_counter()
    job = ch._native.acquire_apply(target, data, hou.time())
    try:
        for _ in range(1024):
            executor.submit(ch._native.prewarm_apply, job).result(timeout=60)
            if not ch._native.expand_apply(job):
                break
        else:
            raise AssertionError("Packed acquisition did not finish")
        turns = 1
        while not ch._native.prepare_apply(target, data, hou.time(), True, job):
            turns += 1
            assert turns < 1024
        executor.submit(ch._native.convert_apply, job).result(timeout=60)
        if not accept:
            return job, {}
        snapshot = ch._native.finish_apply(job)
        return snapshot, dict(seconds=time.perf_counter() - started, turns=turns, stats=ch._native.native_stats(target))
    except BaseException:
        executor.submit(ch._native.discard_apply, job).result(timeout=60)
        raise


def save():
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def check(label, executor, *, material="surface", reuse=None, expected=None):
    global previous
    snapshot, row = capture(handle, update(material), executor)
    info = ch._native.snapshot_info(snapshot, previous) if previous is not None else ch._native.snapshot_info(snapshot)
    assert info["objects"] == points.evalParm("count") * len(members), (label, info)
    assert info["geometry_resources"] == len(members), (label, info)
    assert info["instance_attribute_fields"] >= info["objects"] * 2, (label, info)
    assert "packed_population_reuses" in row["stats"], "Build lacks packed population reuse diagnostics"
    if reuse is not None:
        assert row["stats"]["packed_population_reuses"] == reuse, (label, row)
    if reuse == 1:
        assert row["stats"]["scene_lifecycle"]["retired_objects"] == 0, (label, row)
        assert not row["stats"]["packed_assembly"]["seconds"].get("host_packed_capture"), (label, row)
    if expected:
        for key, value in expected.items():
            assert info["difference"][key] == value, (label, key, info)
    # A real static shutter takes the normal sampled packed path. Its owned
    # scene must match the no-motion population transaction exactly.
    reference_snapshot, _ = capture(oracle, update(material, enabled=True), executor)
    delta = ch._native.snapshot_info(snapshot, reference_snapshot)["difference"]
    assert not any(
        delta[key]
        for key in ("object_upserts", "object_poses", "object_attributes", "object_deletions", "geometry_deletions")
    ), (label, delta)
    row.update(
        label=label,
        inventory=info,
        wire_bytes=len(ch._native.encode_snapshot(snapshot, previous)),
        fallback_frame=float(hou.frame()),
        primitive_source_frame=primitive_source_frame(),
    )
    report["records"].append(row)
    previous = snapshot
    save()


try:
    geometry = copies.geometry()
    assert len(geometry.prims()) == args.count
    assert geometry.findPointAttrib("Cd") is not None and geometry.findPointAttrib("id") is not None
    assert {prim.intrinsicValue("cycles_blender_reference") for prim in geometry.prims()} == {reference}
    with ThreadPoolExecutor(max_workers=1) as executor:
        check("cold", executor, reuse=0)
        for frame in (2, 3, 4):
            hou.setFrame(frame)
            check(
                "unchanged timeline " + str(frame),
                executor,
                reuse=1,
                expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0),
            )
        camera = hou.node("/obj").createNode("cam", "packed_reuse_camera")
        camera.parm("tx").set(0.2)
        camera_snapshot, camera_row = capture(handle, dict(camera_path=camera.path()), executor)
        delta = ch._native.snapshot_info(camera_snapshot, previous)["difference"]
        assert delta["object_upserts"] == delta["object_poses"] == delta["mesh_upserts"] == 0, delta
        assert camera_row["stats"]["packed_population_reuses"] == 0, camera_row
        previous = camera_snapshot
        check(
            "geometry after camera", executor, reuse=1, expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0)
        )
        surface_color = (0.2, 0.7, 0.4)
        check(
            "current material values",
            executor,
            reuse=1,
            expected=dict(material_upserts=1, object_upserts=0, mesh_upserts=0),
        )
        pass_index = 4
        check("object model edit", executor, reuse=0, expected=dict(object_upserts=args.count * 4, mesh_upserts=0))
        members[0]["mesh"] = mesh("packed/mesh:0", size=(0.75, 0.75, 0.75))
        check("same key source replacement", executor, reuse=0, expected=dict(object_upserts=0, mesh_upserts=1))
        check(
            "unchanged replacement", executor, reuse=1, expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0)
        )
        source.parm("ref_frame1").set(2.0)
        assert {prim.intrinsicValue("cycles_blender_frame") for prim in copies.geometry().prims()} == {2.0}
        check(
            "primitive source frame edit",
            executor,
            reuse=0,
            expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0),
        )
        source.parm("ref_frame1").set(1.0)
        check("restore primitive source frame", executor, reuse=0)
        points.parm("tint").set(0.75)
        check(
            "shader-visible Cd edit",
            executor,
            reuse=0,
            expected=dict(object_upserts=0, object_poses=0, object_attributes=args.count * 4, mesh_upserts=0),
        )
        assert report["records"][-1]["stats"]["adapter_attribute_updates"] == args.count * 4
        assert report["records"][-1]["stats"]["adapter_full_object_updates"] == 0
        points.parm("dx").set(0.5)
        check("point placement edit", executor, reuse=0, expected=dict(object_upserts=0, object_poses=args.count * 4))
        obj.parm("tx").set(0.3)
        check("parent transform edit", executor, reuse=0, expected=dict(object_upserts=0, object_poses=args.count * 4))
        check("material binding edit", executor, material="alternate", reuse=0, expected=dict(mesh_upserts=4))
        unaccepted, _ = capture(handle, update("alternate"), executor, accept=False)
        executor.submit(ch._native.discard_apply, unaccepted).result(timeout=60)
        unaccepted = None
        points.parm("dx").set(0.75)
        check(
            "edit after discarded no-op",
            executor,
            material="alternate",
            reuse=0,
            expected=dict(object_upserts=0, object_poses=args.count * 4),
        )
        unaccepted = ch._native.acquire_apply(handle, update("alternate"), hou.time())
        executor.submit(ch._native.prewarm_apply, unaccepted).result(timeout=60)
        assert not ch._native.expand_apply(unaccepted)
        bad = update("alternate")
        bad["motion"]["fps"] = 0
        try:
            ch._native.prepare_apply(handle, bad, hou.time(), True, unaccepted)
        except (ValueError, RuntimeError) as error:
            assert "positive FPS" in str(error), error
        else:
            raise AssertionError("Invalid capture was accepted")
        executor.submit(ch._native.discard_apply, unaccepted).result(timeout=60)
        unaccepted = None
        check("recover rejected preparation", executor, material="alternate", reuse=1)
        previous, _ = capture(handle, dict(deleted_objects=["unknown-explicit-object"]), executor)
        check(
            "unknown explicit deletion invalidates proof",
            executor,
            material="alternate",
            reuse=0,
            expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0),
        )
        request = update("alternate")["geometry"][0]
        deleted, _ = capture(
            handle,
            dict(deleted_objects=[request["object_key"]], deleted_geometry=[request["geometry_key"]]),
            executor,
        )
        info = ch._native.snapshot_info(deleted, previous)
        assert info["objects"] == info["geometry_resources"] == 0, info
        report["explicit_alias_retirement"] = info
        previous = deleted
        check("recreate after explicit aliases", executor, material="alternate", reuse=0)
        empty, _ = capture(handle, dict(geometry=[]), executor)
        info = ch._native.snapshot_info(empty, previous)
        assert info["objects"] == info["geometry_resources"] == 0, info
        report["authoritative_empty_retirement"] = info
        previous = empty
        check("recreate after empty population", executor, material="alternate", reuse=0)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if unaccepted is not None:
        ch._native.discard_apply(unaccepted)
    ch._native.close(handle)
    ch._native.close(oracle)
    save()
