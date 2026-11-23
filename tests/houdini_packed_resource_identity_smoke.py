# SPDX-License-Identifier: Apache-2.0
"""Owned packed-deformation deltas: edits retain slots, splits/merges keep ownership."""

import argparse
import json
import traceback
import hou
import cycles_houdini as ch
from packed_deformation_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
f = Fixture(args.device)
path = f.dest / "resource-identity-report.json"
report = dict(result="running", scope=__doc__, checks=[])
handle = ch._native.create("CPU", f.width, f.height, 1, True, False, False, False, None, True)
previous = None


def capture(name, source=None, geometry=1, objects=4, **options):
    global previous
    update = f.update(sop=source or f.copies, **options)
    snapshot = ch._native.apply(handle, update, options.get("now", 0))
    info = ch._native.snapshot_info(snapshot, previous) if previous is not None else ch._native.snapshot_info(snapshot)
    assert info["geometry_resources"] == geometry and info["objects"] == objects, (name, info)
    packet = (
        ch._native.encode_snapshot(snapshot, previous) if previous is not None else ch._native.encode_snapshot(snapshot)
    )
    report["checks"].append(dict(name=name, inventory=info, wire_bytes=len(packet)))
    previous = snapshot
    return info.get("difference", {})


def unchanged_bindings(delta):
    assert delta["object_upserts"] == delta["object_deletions"] == delta["geometry_deletions"] == 0, delta


try:
    capture("Four copies share one requested/shutter prototype without user IDs")
    for speed in (0.5, 0.75, 1.25):
        f.motion.parm("test_speed").set(speed)
        delta = capture("Shutter-only prototype edit keeps all four object bindings")
        unchanged_bindings(delta)
        assert delta["mesh_upserts"] == 1 and delta["object_poses"] == 0, delta
    for now in (1 / 24, 2 / 24):
        delta = capture("A new nominal frame replaces the shared geometry in its accepted slot", now=now)
        unchanged_bindings(delta)
        assert delta["mesh_upserts"] == 1, delta
    f.motion.parm("test_rows").set(3)
    delta = capture("Prototype topology changes keep the binding slot")
    unchanged_bindings(delta)
    assert delta["mesh_upserts"] == 1, delta
    delta = capture("Warm accepted topology sends no new geometry")
    unchanged_bindings(delta)
    assert delta["mesh_upserts"] == 0, delta
    f.motion.parm("test_rows").set(2)
    delta = capture("Repairing topology keeps all geometry visible")
    unchanged_bindings(delta)
    assert delta["mesh_upserts"] == 1, delta

    left = f.obj.createNode("xform", "left_resource_slot")
    left.setInput(0, f.pack)
    left.parm("tx").set(-0.7)
    right = f.obj.createNode("xform", "right_resource_slot")
    right.setInput(0, f.pack)
    right.parm("tx").set(0.7)
    joined = f.obj.createNode("merge", "split_merge_resource_slots")
    joined.setInput(0, left)
    joined.setInput(1, right)
    alternate = f.obj.createNode("xform", "different_prototype")
    alternate.setInput(0, f.motion)
    alternate.parm("sy").set(0.6)
    alternate_pack = f.obj.createNode("pack", "different_prototype_packed")
    alternate_pack.setInput(0, alternate)
    alternate_pack.parm("pivot").set("origin")
    capture("Two joined instances begin with one compatible prototype", joined, objects=2)
    right.setInput(0, alternate_pack)
    delta = capture("Splitting one prototype creates one additional resource", joined, geometry=2, objects=2)
    assert delta["geometry_deletions"] == delta["object_deletions"] == 0 and delta["object_upserts"] == 1, delta
    alternate.parm("sy").set(0.8)
    delta = capture("Editing one of two distinct prototypes retains both bindings", joined, geometry=2, objects=2)
    unchanged_bindings(delta)
    right.setInput(0, f.pack)
    delta = capture("Merging compatible prototypes retires only the redundant slot", joined, objects=2)
    assert delta["geometry_deletions"] == 1 and delta["object_upserts"] == 1 and delta["object_deletions"] == 0, delta
    f.motion.parm("test_speed").set(0.65)
    delta = capture("Merged instances retain their shared accepted slot after another edit", joined, objects=2)
    unchanged_bindings(delta)
    assert delta["mesh_upserts"] == 1, delta

    empty = ch._native.apply(handle, dict(geometry=[]), 0.0)
    info = ch._native.snapshot_info(empty, previous)
    assert info["geometry_resources"] == info["objects"] == 0, info
    assert info["difference"]["geometry_deletions"] == 1 and info["difference"]["object_deletions"] == 2, info
    report["checks"].append(dict(name="Source retirement removes every remaining geometry/object slot", inventory=info))
    previous = empty
    delta = capture("Recreating the source cannot reuse retired cache ownership", joined, objects=2)
    assert delta["mesh_upserts"] == 1 and delta["object_upserts"] == 2 and delta["geometry_deletions"] == 0, delta
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    ch._native.close(handle)
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
