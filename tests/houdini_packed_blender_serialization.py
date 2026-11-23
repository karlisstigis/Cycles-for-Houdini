# SPDX-License-Identifier: Apache-2.0
"""Run write, reopen, and stale in separate runner-owned Houdini processes.

This fixture publishes synthetic evaluated bounds, so serialization, cache
pressure and invalidation can be checked without launching Blender or rendering.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import hou

from cycles_houdini import _native


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--mode", required=True, choices=("write", "reopen", "stale"))
parser.add_argument("--out", required=True, type=Path)
args = parser.parse_args()
destination = args.out.resolve()
destination.mkdir(parents=True, exist_ok=True)
hou.hipFile.setName(str(destination / "bounds-probe.hip"))

source = destination / "bounds source \u00fcnicode.blend"
linked = destination / "linked library \u7247.blend"
saved = destination / "reference.bgeo.sc"
reference = f"$HIP/{source.name}#object=Cube"
frame = 1.25
expected_box = [-2.0, -3.0, -4.0, 5.0, 6.0, 7.0]
geo = hou.node("/obj").createNode("geo", "bounds_serialization", run_init_scripts=False)
report = dict(result="running", mode=args.mode, pid=os.getpid(), frame=frame, reference=reference)


def versions(path):
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]


if args.mode == "write":
    source.write_bytes(b"Native bounds fixture; never evaluated by Blender")
    linked.write_bytes(b"Linked dependency revision one")
    dependency_versions = {str(path): versions(path) for path in (source, linked)}

    def publish(value):
        record = dict(
            schema_version=1,
            known=True,
            reference=value,
            frame=frame,
            source=str(source),
            selector_type="OBJECT",
            selector=value.split("#object=", 1)[1],
            source_identity=hashlib.sha256(value.encode()).hexdigest(),
            dependency_versions=dependency_versions,
            bounds=expected_box,
        )
        _native.publish_blender_reference_bounds(value, frame, json.dumps(record, allow_nan=False))

    publish(reference)
    node = geo.createNode("cycles::blender_reference")
    node.parm("ref_frame1").deleteAllKeyframes()
    node.setParms(dict(file=f"$HIP/{source.name}", ref_target1="Cube", ref_target_kind1="object", ref_frame1=frame))
    node.cook(force=True)
    geometry = node.geometry()
    primitive = geometry.prim(0)
    assert primitive.intrinsicValue("cycles_blender_bounds_known")
    # Retaining a primitive must keep its verified record alive after resident
    # cache pressure evicts the entry. This exceeds the 1024-entry LRU limit.
    for index in range(1100):
        publish(f"$HIP/{source.name}#object=Eviction{index}")
    assert primitive.intrinsicValue("cycles_blender_bounds_known"), "LRU eviction lost a live primitive's box"
    assert json.loads(primitive.intrinsicValue("cycles_blender_bounds"))["bounds"] == expected_box
    geometry.saveToFile(str(saved))
    report.update(cache_pressure_entries=1100, geometry=str(saved))
    _native.clear_blender_reference_bounds()
    assert not primitive.intrinsicValue("cycles_blender_bounds_known"), "Cache clear resurrected a retained box"
else:
    writer = json.loads((destination / "write.json").read_text())
    assert writer["result"] == "passed" and writer["pid"] != os.getpid(), "Reopen requires a second Houdini process"
    if args.mode == "stale":
        # A linked dependency edit must invalidate the persisted source box even
        # though the primary .blend path, selection and source frame did not change.
        linked.write_bytes(linked.read_bytes() + b"; changed linked revision")
    node = geo.createNode("file")
    node.parm("file").set(str(saved))
    primitive = node.geometry().prim(0)
    assert primitive.intrinsicValue("cycles_blender_frame") == frame
    assert primitive.intrinsicValue("cycles_blender_reference") == reference
    # Packed load is metadata-only: display the saved preview immediately, then
    # validate explicitly. A file SOP has no owning reference-SOP async hook.
    assert not primitive.intrinsicValue("cycles_blender_bounds_known")
    assert primitive.intrinsicValue("cycles_blender_bounds_status") == "unverified"
    encoded = primitive.intrinsicValue("cycles_blender_bounds")
    assert json.loads(encoded)["bounds"] == expected_box
    try:
        _native.publish_blender_reference_bounds(reference, frame, encoded)
    except RuntimeError:
        assert args.mode == "stale", "Unchanged saved dependency signatures were rejected"
    else:
        assert args.mode == "reopen", "Changed linked dependency signatures were accepted"
    known = bool(primitive.intrinsicValue("cycles_blender_bounds_known"))
    assert known == (args.mode == "reopen"), (args.mode, known)
    report.update(cold_process=True, initial_preview_unverified=True, bounds_known=known)
    if known:
        encoded = primitive.intrinsicValue("cycles_blender_bounds")
        assert json.loads(encoded)["bounds"] == expected_box
        malformed = json.loads(encoded)
        del malformed["native_signatures"][str(linked)]
        _native.clear_blender_reference_bounds()
        editable = hou.Geometry()
        editable.merge(node.geometry())
        editable.prim(0).setIntrinsicValue("cycles_blender_bounds", json.dumps(malformed))
        try:
            _native.publish_blender_reference_bounds(reference, frame, json.dumps(malformed))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Native validation accepted a missing linked dependency signature")
        assert not editable.prim(0).intrinsicValue("cycles_blender_bounds_known"), (
            "Missing linked signature was accepted"
        )
        report["malformed_linked_signature_rejected"] = True

report["result"] = "passed"
(destination / f"{args.mode}.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
