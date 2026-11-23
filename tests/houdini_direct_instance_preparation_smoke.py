# SPDX-License-Identifier: Apache-2.0
"""Capture-only direct-instance handoff, motion-path parity and warm phase profile."""

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
parser.add_argument("--count", type=int, default=16)
parser.add_argument("--profile-only", action="store_true")
args = parser.parse_args()
assert args.count >= 2
args.out.mkdir(parents=True, exist_ok=True)
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
obj = hou.node("/obj").createNode("geo", "direct_preparation", run_init_scripts=False)
obj.parmTuple("s").set((-1.2, 0.8, 1.5))
points = obj.createNode("attribwrangle")
points.parm("class").set(0)
for name, template in (
    ("count", hou.IntParmTemplate("count", "Count", 1, default_value=(args.count,))),
    ("dx", hou.FloatParmTemplate("dx", "Placement", 1)),
    ("tint", hou.FloatParmTemplate("tint", "Tint", 1, default_value=(0.25,))),
    ("first_id", hou.IntParmTemplate("first_id", "First ID", 1)),
    ("source_index", hou.FloatParmTemplate("source_index", "Source Index", 1, default_value=(1.0,))),
):
    points.addSpareParmTuple(template)
reference = "fixture.blend#collection=Members"
points.parm("snippet").set(
    'for(int i=0;i<chi("count");i++){int p=addpoint(0,set(i*.25+chf("dx"),0,0));'
    'setpointattrib(0,"instance",p,' + json.dumps(reference) + ");"
    'setpointattrib(0,"index",p,chf("source_index"));setpointattrib(0,"id",p,i+chi("first_id"));'
    'setpointattrib(0,"Cd",p,set(chf("tint"),float(i+chi("first_id"))/100.0,.8));}'
)
members = [
    prototype("direct/member:" + str(index), mesh("direct/mesh:" + str(index)), translate=(0, index * 0.3, 0))[
        "instances"
    ][0]
    for index in range(4)
]
report = dict(result="running", scope=__doc__, count=args.count, records=[], runtime=str(ch.__file__))
handle = ch._native.create("CPU", 8, 8, 1, True, False, False, False, None, True)
oracle = ch._native.create("CPU", 8, 8, 1, True, False, False, False, None, True)
previous = None
job = None
surface_color = (0.5, 0.3, 0.2)
pass_index = 0
ordinary_sop = points


def update(material="surface", enabled=False):
    request = ch.geometry_request(obj, points, material=material, material_mode="uniform")
    source = dict(request)
    request["object_properties"]["pass_index"] = pass_index
    # Public s@instance capture has an ordinary root alongside its suffixed
    # point-instance root, even when the ordinary source renders no primitives.
    ordinary = ch.geometry_request(obj, ordinary_sop, material=material, material_mode="uniform")
    ordinary["object_properties"]["pass_index"] = pass_index
    ordinary["render_types"] = dict(mesh=True, points=False, curves=True, volume=True, packed=True)
    request["object_key"] += "/point-instances"
    request["geometry_key"] += "/point-instances"
    request.update(
        point_instancing=dict(
            mode="fast",
            instancer_path=obj.path(),
            frame=hou.frame(),
            fallback=-1,
            prototypes=[
                dict(
                    path=reference,
                    node_id=0,
                    source_reference=reference,
                    source_frame=frame,
                    request=source,
                    direct_instances=members,
                )
                for frame in (1.0, 2.0)
            ],
        ),
        render_types=dict(mesh=False, points=False, curves=False, packed=True, volume=False),
    )
    return dict(
        geometry=[ordinary, request],
        materials=[dict(key=material, kind="emission", color=surface_color)],
        motion=dict(enabled=enabled, fps=24, shutter_frames=0.5, offset=0, samples=3),
    )


def capture(target, data, executor, accept=True):
    row = {}
    started = time.perf_counter()
    job = ch._native.acquire_apply(target, data, hou.time())
    row["acquire"] = time.perf_counter() - started
    scan = expand = 0.0
    for _ in range(1024):
        at = time.perf_counter()
        executor.submit(ch._native.prewarm_apply, job).result(timeout=60)
        scan += time.perf_counter() - at
        at = time.perf_counter()
        more = ch._native.expand_apply(job)
        expand += time.perf_counter() - at
        if not more:
            break
    else:
        raise AssertionError("Packed acquisition did not finish")
    at = time.perf_counter()
    turns = 1
    while not ch._native.prepare_apply(target, data, hou.time(), True, job):
        turns += 1
        assert turns < 1024
    row.update(scan=scan, expand=expand, prepare=time.perf_counter() - at, turns=turns)
    at = time.perf_counter()
    executor.submit(ch._native.convert_apply, job).result(timeout=60)
    row["convert"] = time.perf_counter() - at
    if not accept:
        return job, row
    at = time.perf_counter()
    snapshot = ch._native.finish_apply(job)
    row.update(finish=time.perf_counter() - at, total=time.perf_counter() - started)
    row["stats"] = ch._native.native_stats(target)
    return snapshot, row


def check(label, executor, material="surface", expected=None, adapter=None):
    global previous
    snapshot, row = capture(handle, update(material), executor)
    info = ch._native.snapshot_info(snapshot, previous) if previous is not None else ch._native.snapshot_info(snapshot)
    ordinary_count = int(ordinary_sop != points)
    assert (
        info["objects"] == points.evalParm("count") * 4 + ordinary_count
        and info["geometry_resources"] == 4 + ordinary_count
    ), (label, info)
    assert info["instance_attribute_fields"] >= points.evalParm("count") * 4 * 3, (label, info)
    if expected:
        for key, value in expected.items():
            assert info["difference"][key] == value, (label, key, info)
    if adapter:
        for key, value in adapter.items():
            assert row["stats"][key] == value, (label, key, row["stats"])
    if "direct_population_reuses" in row["stats"] and label.startswith("unchanged timeline"):
        assert row["stats"]["direct_population_reuses"] == 1, row
        assert row["stats"]["direct_instances_expanded"] == 0, row
        assert row["stats"]["scene_lifecycle"]["retired_objects"] == 0, row
    if not args.profile_only:
        # Enabled, static transform motion deliberately takes the original host
        # path. Its owned objects must match the no-motion worker expansion.
        reference_snapshot, _ = capture(oracle, update(material, enabled=True), executor)
        delta = ch._native.snapshot_info(snapshot, reference_snapshot)["difference"]
        assert not any(
            delta[key]
            for key in ("object_upserts", "object_poses", "object_attributes", "object_deletions", "geometry_deletions")
        ), (label, delta)
    row.update(label=label, inventory=info, wire_bytes=len(ch._native.encode_snapshot(snapshot, previous)))
    previous = snapshot
    report["records"].append(row)
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps({key: row[key] for key in ("label", "prepare", "scan", "convert", "total", "wire_bytes")}),
        flush=True,
    )


try:
    with ThreadPoolExecutor(max_workers=1) as executor:
        check("cold", executor)
        syntheses = ch._native.status(handle)["point_instance_syntheses"]
        retirements = None
        for frame in (2, 3, 4):
            hou.setFrame(frame)
            check(
                "unchanged timeline " + str(frame),
                executor,
                expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0),
            )
            assert ch._native.status(handle)["point_instance_syntheses"] == syntheses
            if "direct_population_reuses" in report["records"][-1]["stats"]:
                current = report["records"][-1]["stats"]["scene_lifecycle"]["retirements"]
                assert retirements is None or retirements == current, (retirements, current)
                retirements = current
        if not args.profile_only:
            surface_color = (0.2, 0.7, 0.4)
            check(
                "current material values", executor, expected=dict(material_upserts=1, object_upserts=0, mesh_upserts=0)
            )
            assert report["records"][-1]["stats"]["direct_population_reuses"] == 1
            camera = hou.node("/obj").createNode("cam", "direct_preparation_camera")
            camera.parm("tx").set(0.2)
            camera_snapshot, camera_row = capture(handle, dict(camera_path=camera.path()), executor)
            delta = ch._native.snapshot_info(camera_snapshot, previous)["difference"]
            assert delta["object_upserts"] == delta["object_poses"] == delta["mesh_upserts"] == 0, delta
            assert camera_row["stats"]["direct_instances_expanded"] == 0, camera_row
            assert camera_row["stats"]["scene_lifecycle"]["retired_objects"] == 0, camera_row
            previous = camera_snapshot
            check(
                "geometry capture after camera",
                executor,
                expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0),
            )
            assert report["records"][-1]["stats"]["direct_population_reuses"] == 1
            ordinary_sop = obj.createNode("box", "ordinary_source_edit")
            check("empty ordinary root starts contributing", executor, expected=dict(object_upserts=1, mesh_upserts=1))
            assert report["records"][-1]["stats"]["direct_population_reuses"] == 0
            assert report["records"][-1]["stats"]["direct_instances_expanded"] == args.count * 4
            ordinary_sop = points
            check("remove ordinary contribution", executor, expected=dict(object_deletions=1, geometry_deletions=1))
            pass_index = 4
            check("object model edit", executor, expected=dict(object_upserts=args.count * 4, mesh_upserts=0))
            assert report["records"][-1]["stats"]["direct_instances_expanded"] == args.count * 4
            members[0]["mesh"] = mesh("direct/mesh:0", size=(0.75, 0.75, 0.75))
            check("same source key replacement", executor, expected=dict(object_upserts=0, mesh_upserts=1))
            assert report["records"][-1]["stats"]["direct_instances_expanded"] == args.count * 4
            # A metadata-only converted capture must not publish navigation or
            # resource state before finish_apply. Discard it, then capture an edit.
            job, _ = capture(handle, update(), executor, accept=False)
            executor.submit(ch._native.discard_apply, job).result(timeout=60)
            points.parm("dx").set(0.125)
            check(
                "latest edit after cancelled no-op",
                executor,
                expected=dict(object_upserts=0, object_poses=args.count * 4, mesh_upserts=0),
            )
            points.parm("dx").set(0)
            check(
                "restore placement after cancel",
                executor,
                expected=dict(object_upserts=0, object_poses=args.count * 4, mesh_upserts=0),
            )
            job = ch._native.acquire_apply(handle, update(), hou.time())
            executor.submit(ch._native.prewarm_apply, job).result(timeout=60)
            assert not ch._native.expand_apply(job)
            bad = update()
            bad["motion"]["fps"] = 0
            try:
                ch._native.prepare_apply(handle, bad, hou.time(), True, job)
            except (ValueError, RuntimeError) as error:
                assert "Native motion requires" in str(error), str(error)
            else:
                raise AssertionError("Invalid capture was accepted")
            executor.submit(ch._native.discard_apply, job).result(timeout=60)
            check("recover rejected capture", executor, expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0))
            assert report["records"][-1]["stats"]["direct_population_reuses"] == 1
            unknown, _ = capture(handle, dict(deleted_objects=["unknown-explicit-object"]), executor)
            previous = unknown
            check(
                "unknown explicit deletion invalidates stamp",
                executor,
                expected=dict(object_upserts=0, object_poses=0, mesh_upserts=0),
            )
            assert report["records"][-1]["stats"]["direct_instances_expanded"] == args.count * 4
        points.parm("tint").set(0.75)
        check(
            "shader-visible field edit",
            executor,
            expected=dict(object_upserts=0, object_poses=0, object_attributes=args.count * 4, mesh_upserts=0),
            adapter=dict(
                adapter_full_object_updates=0, adapter_pose_updates=0, adapter_attribute_updates=args.count * 4
            ),
        )
        if not args.profile_only:
            points.parm("tint").set(0.6)
            points.parm("dx").set(0.125)
            check(
                "mixed pose and attributes use one compact complete command",
                executor,
                expected=dict(
                    object_upserts=0,
                    object_poses=0,
                    object_attributes=args.count * 4,
                    object_attribute_poses=args.count * 4,
                    mesh_upserts=0,
                ),
                adapter=dict(
                    adapter_full_object_updates=0,
                    adapter_pose_updates=0,
                    adapter_attribute_updates=args.count * 4,
                    adapter_pose_attribute_updates=args.count * 4,
                ),
            )
            snippet = points.parm("snippet").evalAsString()
            points.parm("snippet").set(snippet[:-1] + 'setpointattrib(0,"bench_extra",p,0.5);}')
            check(
                "attribute layout addition retains full native update",
                executor,
                expected=dict(object_upserts=args.count * 4, object_attributes=0),
                adapter=dict(adapter_full_object_updates=args.count * 4, adapter_attribute_updates=0),
            )
            points.parm("snippet").set(snippet)
            check(
                "attribute layout removal retains full native update",
                executor,
                expected=dict(object_upserts=args.count * 4, object_attributes=0),
                adapter=dict(adapter_full_object_updates=args.count * 4, adapter_attribute_updates=0),
            )
        points.parm("dx").set(0.5)
        check(
            "point placement edit",
            executor,
            expected=dict(object_upserts=0, object_poses=args.count * 4, mesh_upserts=0),
        )
        obj.parm("tx").set(0.3)
        check(
            "parent transform edit",
            executor,
            expected=dict(object_upserts=0, object_poses=args.count * 4, mesh_upserts=0),
        )
        check(
            "new material binding",
            executor,
            material="alternate",
            expected=dict(object_upserts=args.count * 4, mesh_upserts=4),
        )
        points.parm("count").set(args.count // 2)
        check(
            "population shrink",
            executor,
            material="alternate",
            expected=dict(object_deletions=(args.count - args.count // 2) * 4),
        )
        if not args.profile_only:
            count = points.evalParm("count")
            points.parm("first_id").set(1)
            points.parm("dx").set(0.8)
            check(
                "simultaneous additions deletions and poses",
                executor,
                material="alternate",
                expected=dict(object_deletions=4, object_upserts=4, object_poses=(count - 1) * 4, mesh_upserts=0),
            )
            points.parm("source_index").set(2)
            check(
                "shader-visible source index uses compact attributes",
                executor,
                material="alternate",
                expected=dict(
                    object_deletions=0, object_upserts=0, object_attributes=count * 4, object_poses=0, mesh_upserts=0
                ),
                adapter=dict(
                    adapter_full_object_updates=0, adapter_pose_updates=0, adapter_attribute_updates=count * 4
                ),
            )
        empty, _ = capture(handle, dict(geometry=[]), executor)
        delta = ch._native.snapshot_info(empty, previous)["difference"]
        assert delta["object_deletions"] == (args.count // 2) * 4 and delta["geometry_deletions"] == 4, delta
        previous = empty
        check(
            "recreate retired source",
            executor,
            material="alternate",
            expected=dict(object_upserts=(args.count // 2) * 4, mesh_upserts=4),
        )
        if not args.profile_only:
            ordinary = hou.node("/obj").createNode("geo", "ordinary_preparation", run_init_scripts=False)
            box = ordinary.createNode("box")
            packed = ordinary.createNode("pack")
            packed.setInput(0, box)
            mixed = update("alternate", enabled=True)
            mixed["geometry"][1]["motion_blur"] = False
            native = ch.geometry_request(
                ordinary, packed, material="alternate", material_mode="uniform", motion="deformation"
            )
            alias = mixed["geometry"][1]["geometry_key"]
            native["geometry_key"] = alias
            mixed["geometry"].append(native)
            snapshot, _ = capture(handle, mixed, executor)
            info = ch._native.snapshot_info(snapshot)
            assert info["objects"] == points.evalParm("count") * 4 + 1 and info["geometry_resources"] == 5, info
            # Geometry and placement aliases have independent deletion APIs.
            removed, _ = capture(
                handle,
                dict(
                    deleted_geometry=[alias], deleted_objects=[request["object_key"] for request in mixed["geometry"]]
                ),
                executor,
            )
            info = ch._native.snapshot_info(removed, snapshot)
            assert info["objects"] == info["geometry_resources"] == 0, info
            assert info["difference"]["geometry_deletions"] == 5, info
            report["mixed_root_alias_deletion"] = info
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if job is not None:
        ch._native.discard_apply(job)
    ch._native.close(handle)
    ch._native.close(oracle)
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
