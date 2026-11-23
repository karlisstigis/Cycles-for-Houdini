# SPDX-License-Identifier: Apache-2.0
"""Empty packed motion roots and many-source recycled alias ownership.

Capture-only: compare complete worker payloads and snapshot digests against
serial/full conversion, and compare empty/revived scenes to fresh adapters.
Zero-scale members leave no renderable nominal packed leaves; a separate
case restores them only during shutter samples. Fixed leaves with 1/16/256 roots expose alias planning cost separately
from changing population size. Timings are observations, never pass thresholds.
"""

import argparse
import hashlib
import json
import os
import statistics
import time
import traceback

import hou
import cycles_houdini as ch
from support.paths import artifacts_root

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--section", choices=("empty", "many", "all"), default="all")
parser.add_argument("--leaves", type=int, default=4096)
parser.add_argument("--repeats", type=int, default=5)
parser.add_argument(
    "--render",
    action="store_true",
    default=True,
    help="Verify neighbor/empty/revival pixels against fresh sessions (default)",
)
parser.add_argument("--skip-render", dest="render", action="store_false")
args = parser.parse_args()
native = ch._native
dest = artifacts_root() / "stage3/packed-alias-conversion"
dest.mkdir(parents=True, exist_ok=True)
report = dict(
    result="running",
    device=args.device,
    options=vars(args),
    runtime=json.loads(os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY", "null")),
    empty=[],
    many=[],
)
SWITCHES = ("CYCLES_HOUDINI_SERIAL_CONVERSION", "CYCLES_HOUDINI_FULL_PACKED_CONVERSION")


def mode(name):
    for key in SWITCHES:
        os.environ.pop(key, None)
    if name == "serial":
        os.environ[SWITCHES[0]] = "1"
    if name == "full":
        os.environ[SWITCHES[1]] = "1"


def handle():
    return native.create(args.device, 32, 24, 1, True, True, False, False, None, True)


def convert(session, update, now, schedule):
    mode(schedule)
    started = time.perf_counter()
    if "geometry" in update:
        # Match WorkerSession's retirement order. The synchronous apply API
        # holds its retired snapshot through conversion and correctly refuses
        # recycling as shared; prewarm releases that owner before planning.
        job = native.acquire_apply(session, update, now)
        try:
            native.prewarm_apply(job)
            while native.expand_apply(job):
                native.prewarm_apply(job)
            while not native.prepare_apply(session, update, now, False, job):
                pass
            native.convert_apply(job)
            assert not native.acquire_images(job), "This fixture has no texture images"
            snapshot = native.finish_apply(job)
        except BaseException:
            native.discard_apply(job)
            raise
    else:
        snapshot = native.apply(session, update, now)
    seconds = time.perf_counter() - started
    # Full payloads compare history-independent contents; deltas are covered
    # by the longer recycled-conversion test.
    payload = bytes(native.encode_snapshot(snapshot))
    result = dict(digest=native.snapshot_digest(snapshot), payload=hashlib.sha256(payload).hexdigest())
    status = native.status(session)
    status.update(native.native_stats(session))
    assert not status["error"], (schedule, status)
    return result, seconds, status


def population(parent, name, count):
    obj = parent.createNode("geo", name, run_init_scripts=False)
    points = obj.createNode("line", "points")
    points.setParms(dict(points=count, dist=2))
    strip = obj.createNode("attribwrangle", "points_only")
    strip.setInput(0, points)
    strip.parm("class").set(1)
    strip.parm("snippet").set("removeprim(0,@primnum,0);")
    fields = obj.createNode("attribwrangle", "fields")
    fields.setInput(0, strip)
    fields.parm("class").set(2)
    fields.addSpareParmTuple(hou.IntParmTemplate("empty", "Empty", 1))
    fields.addSpareParmTuple(hou.IntParmTemplate("churn", "Churn", 1))
    fields.addSpareParmTuple(hou.IntParmTemplate("shutter_only", "Shutter only", 1))
    fields.parm("snippet").set(
        "@P.x+=.01*@Frame; f@pscale=1; "
        'if(chi("empty") && (!chi("shutter_only") || abs(@Frame-floor(@Frame+.5))<.01)) f@pscale=0;'
    )
    box = obj.createNode("box", "prototype")
    box.parmTuple("size").set((0.1, 0.1, 0.1))
    copies = obj.createNode("copytopoints", "copies")
    copies.setInput(0, box)
    copies.setInput(1, fields)
    copies.parm("pack").set(1)
    names = obj.createNode("attribwrangle", "names")
    names.setInput(0, copies)
    names.parm("class").set(1)
    names.parm("snippet").set(
        'int id=@primnum; if(chi("../fields/churn") && @primnum==0) id=100000+int(@Frame); s@name=sprintf("leaf%d",id);'
    )
    geometry = names.geometry()
    assert geometry is not None and not names.errors() and len(geometry.prims()) == count, (
        names.errors(),
        fields.errors(),
        copies.errors(),
        points.errors(),
    )
    return obj, names, fields


def update_for(sources, now, motion=False):
    return dict(
        geometry=[
            ch.geometry_request(obj, sop, material="surface", motion="deformation", at_time=now)
            for obj, sop, _ in sources
        ],
        materials=[dict(key="surface", kind="emission", color=(0.2, 0.5, 0.8))],
        motion=dict(enabled=motion, fps=24, shutter_frames=0.5, offset=0, samples=3),
    )


def empty_sequence(shutter_only):
    scene = hou.node("/obj").createNode("subnet", "empty_alias")
    moving = population(scene, "changing", 4)
    neighbor = population(scene, "neighbor", 2)
    moving[2].parm("shutter_only").set(int(shutter_only))
    sessions = {name: handle() for name in ("parallel", "serial", "full")}
    try:
        for frame, empty in enumerate((False, False, False, True, True, False), 1):
            hou.setFrame(frame)
            now = hou.time()
            moving[2].parm("empty").set(int(empty))
            update = update_for([moving, neighbor], now, motion=True)
            rows = [convert(session, update, now, name)[0] for name, session in sessions.items()]
            assert rows[0] == rows[1] == rows[2], (frame, rows)
            # Neighbor remains present. A member found only in shutter samples
            # follows the fresh adapter's visibility policy, never old aliases.
            assert rows[0]["digest"]["objects"] == (2 if empty else 6), (frame, rows)
            fresh = handle()
            try:
                reference = convert(fresh, update, now, "full")[0]
                assert reference == rows[0], (frame, reference, rows)
            finally:
                native.close(fresh)
            report["empty"].append(dict(frame=frame, empty=empty, shutter_only=shutter_only, **rows[0]))
    finally:
        for session in sessions.values():
            native.close(session)
        scene.destroy()


def many_sources(roots):
    assert args.leaves % roots == 0
    scene = hou.node("/obj").createNode("subnet", "many_aliases")
    sources = [population(scene, "source_%03d" % index, args.leaves // roots) for index in range(roots)]
    sessions = {name: handle() for name in ("parallel", "serial", "full")}
    timings = {name: [] for name in sessions}
    planning = {name: [] for name in sessions}
    previous_profile = os.environ.get("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY")
    os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
    recycling = []
    try:
        for frame in range(1, args.repeats + 4):
            hou.setFrame(frame)
            now = hou.time()
            if frame == 4:
                for _, _, fields in sources:
                    fields.parm("churn").set(1)
            update = update_for(sources, now)
            rows = []
            for name, session in sessions.items():
                row, seconds, status = convert(session, update, now, name)
                rows.append(row)
                if frame >= 4:
                    timings[name].append(seconds)
                    phases = status["packed_assembly"]["seconds"]
                    planning[name].append(phases.get("convert_leaves_recycle_merge", 0))
                if name == "parallel" and frame >= 3:
                    assert status["packed_recycled_objects"] > 0, status
                    if frame >= 4:
                        assert status["packed_recycled_objects"] == args.leaves - roots, status
                    recycling.append(status["packed_recycled_objects"])
            assert rows[0] == rows[1] == rows[2], (roots, frame, rows)
            assert rows[0]["digest"]["objects"] == args.leaves, (roots, frame, rows)
        # Alias membership is also observable through explicit root deletion,
        # not only the worker payload emitted while all roots are present.
        deleted = dict(deleted_objects=[update["geometry"][0]["object_key"]])
        removed = [convert(session, deleted, now, name)[0] for name, session in sessions.items()]
        assert removed[0] == removed[1] == removed[2], (roots, "delete_root", removed)
        assert removed[0]["digest"]["objects"] == args.leaves - args.leaves // roots, removed
        revived = [convert(session, update, now, name)[0] for name, session in sessions.items()]
        assert revived[0] == revived[1] == revived[2], (roots, "revive_root", revived)
        assert revived[0]["digest"]["objects"] == args.leaves, revived
        report["many"].append(
            dict(
                roots=roots,
                leaves=args.leaves,
                churn=True,
                recycled=recycling,
                median_seconds={name: statistics.median(values) for name, values in timings.items()},
                median_recycle_plan_merge_seconds={
                    name: statistics.median(values) for name, values in planning.items()
                },
                timings=timings,
                final=rows[0],
                removed=removed[0],
                revived=revived[0],
            )
        )
    finally:
        if previous_profile is None:
            os.environ.pop("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY", None)
        else:
            os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = previous_profile
        for session in sessions.values():
            native.close(session)
        scene.destroy()


def render_sequence(shutter_only):
    import numpy as np

    mode("parallel")
    scene = hou.node("/obj").createNode("subnet", "empty_alias_render")
    moving, neighbor = population(scene, "changing", 4), population(scene, "neighbor", 2)
    moving[2].parm("shutter_only").set(int(shutter_only))
    moving[0].parm("tx").set(-0.5)
    neighbor[0].parm("tx").set(0.5)
    camera = scene.createNode("cam", "camera")
    camera.setParms(dict(tz=6, projection=1, orthowidth=4))
    options = dict(
        device=args.device,
        width=96,
        height=72,
        samples=1,
        threads=2,
        interactive=True,
        collect_diagnostics=True,
        use_texture_cache=False,
        execution="worker",
    )
    totals = []
    try:
        with ch.Session(**options) as session:
            for frame, empty in enumerate((False, True, True, False), 1):
                hou.setFrame(frame)
                moving[2].parm("empty").set(int(empty))
                update = update_for([moving, neighbor], hou.time(), motion=True)
                update.update(camera_path=camera.path(), background=(0, 0, 0))
                image = session.wait(session.apply(update, at_time=hou.time()))
                status = session.status()
                assert not status["error"] and status["state"] == "complete", status
                assert status["packed_recycle_fallback"] != "disabled", status
                with ch.Session(**options) as fresh:
                    reference = fresh.wait(fresh.apply(update, at_time=hou.time()))
                    assert not fresh.status()["error"], fresh.status()
                pixels = np.frombuffer(image["planes"]["combined"]["pixels"], np.float32)
                reference_pixels = np.frombuffer(reference["planes"]["combined"]["pixels"], np.float32)
                assert np.isfinite(pixels).all() and np.array_equal(pixels, reference_pixels), (frame, status)
                rgba = pixels.reshape(72, 96, -1)
                total = float(rgba[:, :, :3].sum())
                assert total > 0, (frame, total)
                totals.append(total)
                report.setdefault("renders", []).append(
                    dict(
                        frame=frame,
                        empty=empty,
                        shutter_only=shutter_only,
                        rgb_sum=total,
                        sha256=hashlib.sha256(pixels.tobytes()).hexdigest(),
                        objects=status["object_resources"],
                        recycled=status["packed_recycled_objects"],
                        fallback=status["packed_recycle_fallback"],
                    )
                )
        assert totals[0] > totals[1] and totals[3] > totals[2], totals
    finally:
        scene.destroy()


try:
    if args.section in ("empty", "all"):
        for shutter_only in (False, True):
            empty_sequence(shutter_only)
    if args.section in ("many", "all"):
        for roots in (1, 16, 256):
            many_sources(roots)
    if args.render and args.section in ("empty", "all"):
        for shutter_only in (False, True):
            render_sequence(shutter_only)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    mode("parallel")
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
assert report["result"] == "passed", report.get("error")
