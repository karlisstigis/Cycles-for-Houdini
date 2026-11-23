# SPDX-License-Identifier: Apache-2.0
"""Resident absolute Blender samples survive timeline, SOP, ROP and renderer changes."""

import argparse
import json
from pathlib import Path
import subprocess

import hou
import cycles_houdini as ch
from cycles_houdini import blender_assets, blend_worker, native_scene, _native
from support.paths import artifacts_root

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--source", type=Path, help="Optional artist .blend with an object named Shape")
p.add_argument("--out", type=Path, default=artifacts_root() / "stage3/blender/resident-reuse.json")
a = p.parse_args()
a.out.parent.mkdir(parents=True, exist_ok=True)
if a.source is None:
    a.source = a.out.parent / "resident-reuse-source.blend"
    code = (
        """import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=.5)
obj=bpy.context.object
obj.name="Shape"
obj.location.z=0
obj.keyframe_insert(data_path="location",frame=1)
obj.location.z=.25
obj.keyframe_insert(data_path="location",frame=15)
bpy.ops.wm.save_as_mainfile(filepath="""
        + repr(str(a.source.resolve()))
        + """)
"""
    )
    from cycles_houdini.blender.blend_worker import blender_environment

    # Blender's own environment, as the bridge starts it: not Houdini's OCIO or Python.
    result = subprocess.run(
        [str(blender_assets._blender()), "--background", "--factory-startup", "--python-expr", code],
        capture_output=True,
        text=True,
        timeout=90,
        env=blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
    )
    assert result.returncode == 0, result.stdout + result.stderr
source = a.source.resolve(strict=True)
reference = source.as_posix() + "#object=Shape"
obj = hou.node("/obj").createNode("geo", "resident_samples", run_init_scripts=False)
points = obj.createNode("attribwrangle", "points")
points.parm("class").set(0)
points.setDisplayFlag(True)
points.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam")
camera.parm("tz").set(8)
rop = hou.node("/out").createNode("cycles_ipr", "resident_samples")
rop.setParms(
    dict(
        camera=camera.path(),
        source_mode="objects",
        objectroot="/obj",
        vobject=obj.path(),
        forceobject=obj.path(),
        lights="",
        resx=32,
        resy=32,
        samples=1,
        ipr_samples=1,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
        render_threads=2,
    )
)
rows = []
calls = []
batches = []
evaluate = blend_worker.evaluate_many


def counted(source, executable, requests, *args, **kwargs):
    batches.append([request["frame"] for request in requests])
    calls.extend(request["frame"] for request in requests)
    return evaluate(source, executable, requests, *args, **kwargs)


blend_worker.evaluate_many = counted


def author(frames, with_index=True):
    points.parm("snippet").set(
        "float frames[]={" + ",".join(map(str, frames)) + "}; "
        "foreach(int i;float frame;frames){int p=addpoint(0,set(i*.5,0,0));"
        'setpointattrib(0,"instance",p,'
        + json.dumps(reference)
        + ");"
        + ('setpointattrib(0,"index",p,frame);' if with_index else "")
        + "}"
    )


def capture(label, expected, session=None, stable_synthesis=False):
    before = len(calls)
    syntheses = _native.status(session._handle)["point_instance_syntheses"] if session is not None else None
    update = native_scene.capture(rop)
    assert calls[before:] == expected, (label, calls[before:], expected)
    assert not any("unavailable" in item["code"] for item in update["source_diagnostics"]), update
    row = dict(label=label, evaluated=calls[before:])
    if session is not None:
        image = session.wait(session.apply(update), timeout=120)
        status = session.status()
        host = _native.status(session._handle)
        assert image["complete"] and not status["error"], status
        if stable_synthesis:
            assert host["point_instance_syntheses"] == syntheses, (label, syntheses, host)
        row.update(syntheses=host["point_instance_syntheses"], cache_hits=host["point_instance_cache_hits"])
    rows.append(row)


try:
    hou.setFrame(1)
    # An explicit multi-row bounds refresh and later render preparation share
    # one evaluation per canonical sample, including native buffer adoption.
    from cycles_houdini.blender.blend_bounds import refresh_many

    author([100.25, 101.5])
    job = refresh_many([(reference, frame, True) for frame in (100.25, 101.5)])
    assert job.done.wait(120), "Bounds refresh did not finish"
    assert job.error is None, job.error
    assert batches == [[100.25, 101.5]], batches
    capture("bounds-batch-render-handoff", [])
    assert batches == [[100.25, 101.5]], batches
    assert not job.raw and not job.directories
    author([1, 15])
    with ch.Session(
        device="CPU", width=32, height=32, samples=1, threads=2, execution="worker", collect_diagnostics=True
    ) as session:
        capture("cold", [1.0, 15.0], session)
        capture("unchanged", [], session, True)
        hou.setFrame(9.5)
        capture("absolute-index-timeline", [], session, True)
        author([15, 1])
        capture("reassignment", [], session)
        author([1, 15, 20.25])
        capture("one-new-fractional", [20.25], session)
        author([1, 15])
        capture("return", [], session)
    # A fresh renderer and ROP consumer reuse the same host samples.
    other = rop.parent().copyItems([rop])[0]
    rop = other
    with ch.Session(device="CPU", width=32, height=32, samples=1, threads=2, execution="worker") as session:
        capture("renderer-and-rop-restart", [], session)
    other_obj = obj.parent().copyItems([obj])[0]
    rop.setParms(dict(vobject=other_obj.path(), forceobject=other_obj.path()))
    capture("another-sop", [])
    rop.setParms(dict(vobject=obj.path(), forceobject=obj.path()))
    author([0], with_index=False)
    capture("missing-index-default-frame", [9.5])
    hou.setFrame(10.5)
    capture("missing-index-follows-timeline", [10.5])
    author([1, 15])
    blend_worker.invalidate(str(source))
    capture("forced-reload", [1.0, 15.0])
    state = blender_assets.cache_diagnostics(workers=True)
    assert state["prototypes"]["target_bytes"] == 1024**3, state
    report = dict(result="passed", host=ch.__file__, houdini=hou.applicationVersionString(), rows=rows, cache=state)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
finally:
    blend_worker.evaluate_many = evaluate
    blend_worker.shutdown_all()
