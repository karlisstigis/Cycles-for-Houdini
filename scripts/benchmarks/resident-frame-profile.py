# SPDX-License-Identifier: Apache-2.0
"""Capture/serialization profile of resident external source-frame changes; no render."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
from time import perf_counter
import traceback

import hou
import cycles_houdini as ch
from cycles_houdini import blender_assets, blend_worker, native_scene


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--count", type=int, default=51294)
parser.add_argument("--target", default="wine_bottles_01")
parser.add_argument("--form", choices=("attribute", "packed"), default="attribute")
# The default scene and library are private artist files below $CYCLES_HOUDINI_ARTIST_SCENES.
scenes = Path(os.environ["CYCLES_HOUDINI_ARTIST_SCENES"]) if os.environ.get("CYCLES_HOUDINI_ARTIST_SCENES") else None
parser.add_argument("--hip", type=Path, default=scenes / "Cycles_Test/Blender_Instancing.hiplc" if scenes else None)
parser.add_argument(
    "--blend", type=Path, default=scenes / "Cycles_Test/assets/Instance_Source.blend" if scenes else None
)
args = parser.parse_args()
if args.hip is None or args.blend is None:
    parser.error("give --hip and --blend, or set CYCLES_HOUDINI_ARTIST_SCENES to the folder that holds Cycles_Test/")
args.out.mkdir(parents=True, exist_ok=True)
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
source_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.hip, args.blend)}
copied = args.out / ("isolated-" + args.hip.name)
shutil.copy2(args.hip, copied)
try:
    hou.hipFile.load(str(copied), suppress_save_prompt=True)
except hou.LoadWarning:
    pass
hou.putenv("HIP", str(args.hip.parent))
hou.setFrame(1)
rotation = hou.node("/obj/null1").parm("ry")
value = rotation.eval()
rotation.deleteAllKeyframes()
rotation.set(value)
obj = hou.node("/obj/geo1") or hou.node("/obj/bottles")
points, scatter = obj.node("pointwrangle1"), obj.node("scatter1")
scatter.bypass(False)
points.setInput(0, scatter)
for name, value in (("forcetotal", 1), ("npts", args.count)):
    scatter.parm(name).deleteAllKeyframes()
    scatter.parm(name).set(value)
kind = "object" if args.target == "Shape" else "collection"
reference = args.blend.resolve().as_posix() + "#" + kind + "=" + args.target
points.parm("snippet").set(
    "s@instance="
    + json.dumps(reference)
    + "; f@index=(int(@Frame)%2 ? 1.0 : 2.0); matrix3 m=ident(); 3@transform=m*.2;"
)
output = points
if args.form == "packed":
    source = obj.createNode("cycles::blender_reference", "profile_reference")
    source.parm("frame").deleteAllKeyframes()
    source.setParms(dict(file=args.blend.resolve().as_posix(), target_kind=kind, target=args.target))
    source.parm("frame").setExpression("if($F%2,1,2)", hou.exprLanguage.Hscript)
    remove = obj.createNode("attribdelete")
    remove.setInput(0, points)
    remove.parm("ptdel").set("instance index")
    output = obj.createNode("copytopoints", "profile_copies")
    output.setInput(0, source)
    output.setInput(1, remove)
    output.parm("pack").set(0)
output.setRenderFlag(True)
output.setDisplayFlag(True)
rop = hou.node("/out/cycles_render") or hou.node("/out/cam_1")
rop.setParms(
    dict(source_mode="objects", objectroot="/obj", vobject=obj.path(), forceobject=obj.path(), allowmotionblur=0)
)
blender_assets.prepare_prototypes([(reference, 1.0, True), (reference, 2.0, True)])
evaluations = []
original = blend_worker.evaluate_many


def evaluate(source, executable, requests, *positional, **keywords):
    evaluations.extend(requests)
    return original(source, executable, requests, *positional, **keywords)


blend_worker.evaluate_many = evaluate
report = dict(result="running", runtime=ch.__file__, form=args.form, target=args.target, count=args.count, rows=[])
handle = ch._native.create("CPU", 8, 8, 1, True, False, False, False, None, True)
previous = job = None
try:
    with ThreadPoolExecutor(max_workers=1) as executor:
        for frame in (1, 2, 3, 4):
            hou.setFrame(frame)
            started = perf_counter()
            payload = native_scene.capture(rop)
            payload.pop("_blender_prototypes", None)
            row = dict(
                host_frame=frame, source_frame=1 if frame % 2 else 2, python_capture_seconds=perf_counter() - started
            )
            at = perf_counter()
            job = ch._native.acquire_apply(handle, payload, hou.time())
            row["acquisition_seconds"] = perf_counter() - at
            at = perf_counter()
            for _ in range(1024):
                executor.submit(ch._native.prewarm_apply, job).result(timeout=120)
                if not ch._native.expand_apply(job):
                    break
            else:
                raise AssertionError("Acquisition did not finish")
            row["scan_expand_seconds"] = perf_counter() - at
            at = perf_counter()
            while not ch._native.prepare_apply(handle, payload, hou.time(), True, job):
                pass
            row["preparation_seconds"] = perf_counter() - at
            at = perf_counter()
            executor.submit(ch._native.convert_apply, job).result(timeout=120)
            row["conversion_seconds"] = perf_counter() - at
            at = perf_counter()
            snapshot = ch._native.finish_apply(job)
            row["finish_seconds"] = perf_counter() - at
            row["capture_total_seconds"] = perf_counter() - started
            row["native"] = ch._native.native_stats(handle)
            row["snapshot"] = (
                ch._native.snapshot_info(snapshot, previous)
                if previous is not None
                else ch._native.snapshot_info(snapshot)
            )
            at = perf_counter()
            encoded = ch._native.encode_snapshot(snapshot, previous)
            row.update(encode_seconds=perf_counter() - at, encoded_bytes=len(encoded), evaluations=len(evaluations))
            previous = snapshot
            report["rows"].append(row)
            (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        key: row[key]
                        for key in (
                            "host_frame",
                            "capture_total_seconds",
                            "conversion_seconds",
                            "encode_seconds",
                            "encoded_bytes",
                            "evaluations",
                        )
                    }
                ),
                flush=True,
            )
    assert not evaluations, evaluations
    assert source_hashes == {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.hip, args.blend)
    }
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if job is not None:
        ch._native.discard_apply(job)
    ch._native.close(handle)
    blend_worker.evaluate_many = original
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
