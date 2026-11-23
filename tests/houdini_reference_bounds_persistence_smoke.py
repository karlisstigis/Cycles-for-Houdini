# SPDX-License-Identifier: Apache-2.0
"""Run create then open in fresh hython processes; no Blender or GPU is used."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
import hou
import cycles_houdini as ch
from cycles_houdini.blender import blend_bounds, blend_preview, blend_worker, node_bounds

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--stage", choices=("all", "create", "open", "relocate"), default="all")
parser.add_argument("--hull", action="store_true", help="Persist whole and per-member collection hull previews")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
if args.stage == "all":
    evidence = []
    for stage in ("create", "relocate", "open"):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--out", str(args.out.resolve()), "--stage", stage]
            + (["--hull"] if args.hull else []),
            capture_output=True,
            text=True,
            timeout=60,
        )
        (args.out / (stage + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
        (args.out / (stage + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError("Bounds " + stage + " failed; see " + str(args.out / (stage + ".stderr.log")))
        evidence.append(json.loads(result.stdout.strip().splitlines()[-1]))
    (args.out / "report.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(dict(fresh_process_stages=evidence)))
    raise SystemExit(0)
scene = args.out.resolve() / "saved-bounds.hip"
expected = [-2.0, -3.0, -4.0, 5.0, 6.0, 7.0]


def no_blender(*args, **kwargs):
    raise AssertionError("Bounds save/load/validation must never evaluate Blender")


blend_worker.evaluate_many = no_blender


def settle():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        node_bounds.drain_pending_node_bounds()
        if not any(
            state["pending"]
            for states in (node_bounds._subscribers, node_bounds._shape_subscribers)
            for state in states.values()
        ):
            return
        time.sleep(0.01)
    raise AssertionError("Saved bounds validation timed out")


def box(node):
    value = node.geometry().boundingBox()
    return [*value.minvec(), *value.maxvec()]


def hull_pieces(node):
    primitive = node.geometry().prim(0)
    encoded = primitive.intrinsicValue("cycles_blender_viewport_hull")
    if encoded:
        pieces = json.loads(encoded)["pieces"]
        assert primitive.intrinsicValue("cycles_blender_viewport_hull_points") == sum(
            len(p["points"]) // 3 for p in pieces
        )
        assert primitive.intrinsicValue("cycles_blender_viewport_hull_triangles") == sum(
            len(p["triangles"]) // 3 for p in pieces
        )
    return json.loads(encoded)["pieces"] if encoded else []


def tetra(x=0):
    return dict(
        points=[-2 + x, -3, -4, 5 - x, -3, -4, -2 + x, 6, -4, -2 + x, -3, 7],
        triangles=[0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3],
    )


if args.stage == "create":
    hou.hipFile.setName(str(scene))
    source = args.out.resolve() / "asset.blend"
    linked = args.out.resolve() / "linked.blend"
    source.write_bytes(b"filesystem-only bounds fixture")
    linked.write_bytes(b"linked dependency")
    versions = {}
    for path in (source, linked):
        stat = path.stat()
        versions[str(path)] = [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]
    obj = hou.node("/obj").createNode("geo", "bounds_fixture", run_init_scripts=False)
    node = obj.createNode("cycles::blender_reference", "reference")
    node.setParms(dict(file="$HIP/asset.blend", ref_target1="Cube", shape_frame=1.25))
    node.parm("ref_frame1").deleteAllKeyframes()
    node.parm("ref_frame1").set(1.25)
    if args.hull:
        node.setParms(dict(viewport_shape="hull", ref_target_kind1="collection"))
    node.cook(force=True)
    reference = node.geometry().prim(0).intrinsicValue("cycles_blender_reference")
    record = dict(
        schema_version=1,
        known=True,
        source=str(source),
        selector_type="COLLECTION" if args.hull else "OBJECT",
        selector="Cube",
        source_identity=hashlib.sha256(b"fixture").hexdigest(),
        bounds=expected,
        dependency_versions=versions,
        frame=1.25,
    )
    if args.hull:
        record["viewport_hulls"] = dict(whole=tetra(), members=[tetra(), tetra(0.25)], max_vertices=64)
    blend_bounds.publish_record(reference, 1.25, record)

    def restore_shape():
        native = ch.load()
        base = json.loads(native.serialized_blender_reference_bounds(reference, 1.25))
        preview = dict(base, max_hull_vertices=64)
        if args.hull:
            preview["viewport_hulls"] = record["viewport_hulls"]
        native.restore_blender_reference_preview(reference, 1.25, preview["source_identity"])
        blend_preview.persist(node, [preview])

    restore_shape()
    node_bounds.drain_pending_node_bounds()
    assert box(node) == expected, box(node)
    assert node.geometry().prim(0).intrinsicValue("cycles_blender_bounds_status") == "validated"
    if args.hull:
        assert len(hull_pieces(node)) == 1
        node.parm("ref_hull_per_member1").set(1)
        node.cook(force=True)
        assert len(hull_pieces(node)) == 2
        # Copies retain the same lightweight hull metadata, with independent placement.
        merge = obj.createNode("merge", "repeated_hulls")
        merge.setInput(0, node)
        merge.setInput(1, node)
        assert len(merge.geometry().prims()) == 2
        assert merge.geometry().prim(0).intrinsicValue("cycles_blender_viewport_hull") == merge.geometry().prim(
            1
        ).intrinsicValue("cycles_blender_viewport_hull")
        # Hold existing details across invalidation: a recook would otherwise
        # replace the primitive and hide a stale shared-implementation bug.
        for invalidation in ("unknown_publication", "preview_invalidation", "cache_clear"):
            node.cook(force=True)
            merge.cook(force=True)
            retained = [node.geometry(), merge.geometry()]
            saved_geometry = args.out.resolve() / (invalidation + ".bgeo.sc")
            node.geometry().saveToFile(str(saved_geometry))
            disk = obj.createNode("file", invalidation + "_file")
            disk.parm("file").set(str(saved_geometry))
            retained.append(disk.geometry())
            for geometry in retained:
                for primitive in geometry.prims():
                    assert primitive.intrinsicValue("cycles_blender_viewport_hull_points") == 8
                    assert primitive.intrinsicValue("cycles_blender_viewport_hull_triangles") == 8
            native = ch.load()
            if invalidation == "unknown_publication":
                native.publish_blender_reference_bounds(
                    reference, 1.25, json.dumps(dict(known=False, reference=reference, frame=1.25))
                )
            elif invalidation == "preview_invalidation":
                native.invalidate_blender_reference_preview(reference, 1.25, record["source_identity"])
            else:
                native.clear_blender_reference_bounds()
            for geometry in retained:
                for primitive in geometry.prims():
                    assert primitive.intrinsicValue("cycles_blender_bounds_known") == (
                        invalidation == "preview_invalidation"
                    ), invalidation
                    preview_alive = invalidation == "unknown_publication"
                    assert bool(primitive.intrinsicValue("cycles_blender_viewport_hull")) == preview_alive, invalidation
                    assert primitive.intrinsicValue("cycles_blender_viewport_hull_points") == (
                        8 if preview_alive else 0
                    ), invalidation
                    assert primitive.intrinsicValue("cycles_blender_viewport_hull_triangles") == (
                        8 if preview_alive else 0
                    ), invalidation
            # Each validity domain retires independently. Render-only unknown
            # records leave the cosmetic shape alive; preview invalidation
            # leaves render authority intact; a context clear retires both.
            # Do not recook held details before checking those transitions.
            # The open/relocate stages below test actual stale dependencies.
            # A render publication cannot restore a retired shape. Explicit
            # fresh preview adoption creates new tokens without reactivating
            # the old held geometry.
            blend_bounds.publish_record(reference, 1.25, record)
            for geometry in retained:
                assert all(
                    bool(p.intrinsicValue("cycles_blender_viewport_hull")) == (invalidation == "unknown_publication")
                    for p in geometry.prims()
                ), "Render publication changed independent preview validity"
            restore_shape()
            if invalidation != "unknown_publication":
                assert all(
                    not p.intrinsicValue("cycles_blender_viewport_hull")
                    for geometry in retained
                    for p in geometry.prims()
                ), "Old held previews were reactivated"
            node_bounds.drain_pending_node_bounds()
            node.cook(force=True)
            assert len(hull_pieces(node)) == 2
    persisted = node_bounds.decode_records(node.parm("saved_bounds").unexpandedString())
    assert len(persisted) == 1 and persisted[0]["reference"].startswith("$HIP/"), persisted
    # HIP save serializes the node record without waiting or evaluating Blender.
    hou.hipFile.save(str(scene))
    print(json.dumps(dict(stage="create", scene=str(scene), persisted_records=len(persisted))))
else:
    # Loading a HIP cannot restore the process-local native cache.
    hou.hipFile.load(str(scene), suppress_save_prompt=True)
    node = hou.node("/obj/bounds_fixture/reference")
    assert box(node) == expected, box(node)
    primitive = node.geometry().prim(0)
    assert primitive.intrinsicValue("cycles_blender_bounds_status") == "unverified"
    assert not primitive.intrinsicValue("cycles_blender_bounds_known")
    if args.hull:
        assert node.evalParm("viewport_shape") == "hull"
        assert node.evalParm("ref_hull_per_member1") == 1
        assert len(hull_pieces(node)) == 2, "Saved hull preview missing before validation"
    settle()
    assert node.geometry().prim(0).intrinsicValue("cycles_blender_bounds_status") == "validated"
    if args.hull:
        assert len(hull_pieces(node)) == 2, "Saved hull disappeared after asynchronous dependency validation"
    native_record = json.loads(node.geometry().prim(0).intrinsicValue("cycles_blender_bounds"))
    stale_record = dict(native_record, native_signatures={path: "stale" for path in native_record["native_signatures"]})
    try:
        ch.load().publish_blender_reference_bounds(
            native_record["reference"], native_record["frame"], json.dumps(stale_record)
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Native publication recaptured and accepted a stale saved signature")
    if args.stage == "relocate":
        moved = args.out.resolve() / "relocated" / "saved-bounds.hip"
        moved.parent.mkdir(exist_ok=True)
        # No save callback: changing HIP then cooking must itself invalidate the
        # old raw-reference cache before the changed-context cook is observable.
        hou.hipFile.setName(str(moved))
        node.cook(force=True)
        assert not node.geometry().prim(0).intrinsicValue("cycles_blender_bounds_known")
        settle()
        assert node.parm("saved_bounds_status").evalAsString() == "stale"
        print(json.dumps(dict(stage="relocate", old_authoritative_bounds_retired_before_cook_return=True)))
        raise SystemExit(0)
    node.parm("ref_frame1").set(2.5)
    assert box(node) == expected, "Source Frame changed the frozen Shape Frame bounds"
    assert not node.geometry().prim(0).intrinsicValue("cycles_blender_bounds_known")
    if args.hull:
        assert len(hull_pieces(node)) == 2, "Source Frame changed the frozen hull"
    node.parm("shape_frame").set(2.5)
    assert box(node) == [-0.5, -0.5, -0.5, 0.5, 0.5, 0.5]
    if args.hull:
        assert not hull_pieces(node), "Missing Shape Frame failed to fall back to a box"
    node.parm("ref_frame1").set(1.25)
    node.parm("shape_frame").set(1.25)
    node.cook(force=True)
    (args.out.resolve() / "linked.blend").unlink()
    # Force a fresh validation context, then observe stale metadata retirement.
    node_bounds._subscribers.clear()
    node_bounds._shape_subscribers.clear()
    node.cook(force=True)
    settle()
    assert node.parm("saved_bounds_status").evalAsString() == "stale"
    assert box(node) == [-0.5, -0.5, -0.5, 0.5, 0.5, 0.5]
    assert node_bounds.decode_records(node.parm("saved_bounds").unexpandedString()) == []
    if args.hull:
        assert not hull_pieces(node), "Stale hull remained active"
    print(
        json.dumps(
            dict(
                stage="open",
                fresh_process_preview=True,
                no_blender=True,
                validated_dependencies=True,
                changed_frame_unknown=True,
                source_frame_preserves_shape=True,
                missing_shape_frame_unknown=True,
                missing_link_stale=True,
            )
        )
    )
