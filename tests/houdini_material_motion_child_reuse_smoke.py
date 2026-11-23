# SPDX-License-Identifier: Apache-2.0
"""Material-motion probe walk: reused child results equal the full walk, byte for byte.

The probe walk reuses a completed child node for packed children with the same
prototype, point prototype, material assignment and descendant stylesheet
(material_motion.cpp, Capture::node). CYCLES_HOUDINI_FULL_MATERIAL_MOTION=1 is
the diagnostic full walk that builds every child request. Both run in one
process, one render session each; images, update counters and diagnostics
must be equal at every step (snapshot digests are not comparable across two
sessions: material keys differ per session). Covered: children sharing prototype and
assignment that differ in stylesheet (twelve per-instance style rules, four
distinct values), a style edit, frame changes with shutter displacement
sampling, and a point instancer whose points alternate between two prototype
objects. Point-prototype slots are deduplicated by OBJ, so two children that
differ only in point prototype also differ in prototype detail; the key carries
both anyway.
"""

import argparse, hashlib, json, os, traceback
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene
from stylesheet_storage_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
f = Fixture(args.device, heavy=False)
f.enable_material_motion(displacement=True)
animated = f.obj.createNode("attribwrangle", "animated_prototype")
animated.setInput(0, f.prototype)
animated.parm("snippet").set("@P.z+=0.01*sin(@Time*3);")
f.copies.setInput(0, animated)
prototypes = []
for name, rows in (("prototype_a", 3), ("prototype_b", 4)):
    geo = f.scene.createNode("geo", name, run_init_scripts=False)
    grid = geo.createNode("grid", "shape")
    grid.setParms(dict(type=0, orient=0, rows=rows, cols=rows))
    grid.parmTuple("size").set((0.8, 0.8))
    moving = geo.createNode("attribwrangle", "moving")
    moving.setInput(0, grid)
    moving.parm("snippet").set("@P.z+=0.01*sin(@Time*5);")
    moving.setDisplayFlag(True)
    moving.setRenderFlag(True)
    geo.parm("shop_materialpath").set(f.material.path())
    geo.setDisplayFlag(False)
    prototypes.append(geo)
instancer = f.scene.createNode("instance", "point_instances")
for child in instancer.children():
    child.destroy()
points = instancer.createNode("attribwrangle", "points")
points.parm("class").set(0)  # detail
points.parm("snippet").set(
    "for (int i = 0; i < 6; ++i) { int p = addpoint(0, set(-3 + 1.2 * i, -4, 0));"
    ' setpointattrib(0, "instance", p, i %% 2 ? "%s" : "%s"); }' % (prototypes[1].path(), prototypes[0].path())
)
points.setDisplayFlag(True)
points.setRenderFlag(True)
instancer.parm("ptinstance").set("fast")
instancer.parm("shop_materialpath").set(f.material.path())
report = dict(result="running", device=args.device, runs={})
destination = f.dest / "material-motion-child-reuse"
destination.mkdir(parents=True, exist_ok=True)
steps = (
    "Per-instance styles, shared prototype and assignment",
    "Frame change",
    "Style edit",
    "Frame change after style edit",
    "Uniform styles",
)


def edit(label):
    if label == steps[0]:
        hou.setFrame(1)
        f.styles("colors")
    elif label == "Frame change":
        hou.setFrame(2)
    elif label == "Style edit":
        f.styles("colors", offset=0.05)
    elif label == "Frame change after style edit":
        hou.setFrame(3)
    elif label == "Uniform styles":
        f.styles("uniform")


def run(full):
    os.environ["CYCLES_HOUDINI_FULL_MATERIAL_MOTION"] = "1" if full else "0"
    records = []
    try:
        with ch.Session(
            device=args.device,
            width=f.width,
            height=f.height,
            samples=1,
            threads=2,
            use_texture_cache=False,
            collect_diagnostics=True,
        ) as session:
            for label in steps:
                edit(label)
                frame = session.wait(session.apply(f.update(), at_time=hou.time()), timeout=90)
                status = session.status()
                assert frame["complete"] and not status["error"], (label, status)
                plane = frame["planes"]["combined"]
                records.append(
                    dict(
                        label=label,
                        digest=hashlib.sha256(plane["pixels"]).hexdigest(),
                        samples=status["material_motion_samples"],
                        objects=status["object_updates"],
                        diagnostics=json.dumps(status["source_diagnostics"], sort_keys=True, default=str),
                        hold=status.get("material_motion_cache_hits"),
                    )
                )
    finally:
        os.environ.pop("CYCLES_HOUDINI_FULL_MATERIAL_MOTION", None)
    return records


try:
    full, reuse = run(True), run(False)
    report["runs"] = dict(full=full, reuse=reuse)
    for a, b in zip(full, reuse, strict=True):
        assert a["samples"] > 0, a  # The probe found displacement and sampled the shutter.
        assert a == b, (a, b)
    # The edits reach the image: per-instance styles and the style edit change it.
    assert full[1]["digest"] != full[2]["digest"], "style edit did not change the image"
    assert full[3]["digest"] != full[4]["digest"], "uniform styles did not change the image"
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2, default=str))
print(json.dumps(dict(result=report["result"], runs=report["runs"]), default=str))
