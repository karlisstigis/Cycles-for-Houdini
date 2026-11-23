# SPDX-License-Identifier: Apache-2.0
"""Parallel packed conversion equals the serial schedule, step for step.

Thousands of named packed instances of six prototypes with per-instance Cd and
a numeric shader field go through frame changes (pose only), then an attribute
edit, deletions, a reorder, a prototype edit, a material change and pose-only
frames again (including a return to an earlier frame). Every step is rendered
once with CYCLES_HOUDINI_SERIAL_CONVERSION=1 and once with the parallel
schedule; images must be bitwise equal and the update counters identical.
"""

from support.paths import artifacts_root
import argparse
import hashlib
import json
import os
import traceback
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
dest = artifacts_root() / "stage3/packed-parallel-conversion"
dest.mkdir(parents=True, exist_ok=True)
width, height = 160, 120
report = dict(result="running", device=device, runs={})

scene = hou.node("/obj").createNode("subnet", "parallel_conversion")
obj = scene.createNode("geo", "population", run_init_scripts=False)
targets = obj.createNode("grid", "placements")
targets.setParms(dict(type=0, orient=0, rows=70, cols=70))
targets.parmTuple("size").set((7, 7))
moving = obj.createNode("attribwrangle", "moving_placements")
moving.setInput(0, targets)
moving.parm("class").set(2)  # points
moving.parm("snippet").set(
    "float t=@Frame; @P.z+=0.05*sin(t+@ptnum*0.37); v@v=set(0.3*cos(t+@ptnum),0.2,0.05*cos(t+@ptnum*0.37));"
    "i@variant=@ptnum%6; f@pscale=0.04+0.01*(@ptnum%3);"
)
prototypes = []
for index, (kind, parms) in enumerate(
    (
        ("line", dict(dist=1.5)),
        ("circle", dict(type=1, divs=12)),
        ("grid", dict(rows=2, cols=2)),
        ("sphere", dict(type=2, rows=6, cols=8)),
        ("box", {}),
        ("tube", dict(type=1, rows=2, cols=8)),
    )
):
    node = obj.createNode(kind, "prototype_%d" % index)
    node.setParms(parms)
    prototypes.append(node)
merge = obj.createNode("merge", "all_copies")
for index, prototype in enumerate(prototypes):
    selected = obj.createNode("attribwrangle", "placements_%d" % index)
    selected.setInput(0, moving)
    selected.parm("class").set(2)  # points
    selected.parm("snippet").set("if(i@variant!=%d) removepoint(0, @ptnum);" % index)
    copies = obj.createNode("copytopoints", "copies_%d" % index)
    copies.setInput(0, prototype)
    copies.setInput(1, selected)
    copies.parm("pack").set(1)
    merge.setInput(index, copies)
fields = obj.createNode("attribwrangle", "instance_fields")
fields.setInput(0, merge)
fields.parm("class").set(1)  # primitives
fields.addSpareParmTuple(hou.FloatParmTemplate("test_offset", "Colour Offset", 1))
fields.parm("snippet").set(
    's@name=sprintf("instance%d", @primnum); v@Cd=set(0.2+0.6*((@primnum%11)/10.0),0.3,0.5);'
    'f@rand=0.1+0.8*((@primnum%17)/16.0); if(@primnum%7==0) v@Cd.x+=chf("test_offset");'
)
deleted = obj.createNode("attribwrangle", "deleted_instances")
deleted.setInput(0, fields)
deleted.parm("class").set(1)  # primitives
deleted.parm("snippet").set("if(@primnum%13==0) removeprim(0, @primnum, 1);")
deleted.bypass(True)
ordered = obj.createNode("sort", "reordered_instances")
ordered.setInput(0, deleted)
order_items = ordered.parm("primsort").menuItems()
ordered.parm("primsort").set(next(i for i, item in enumerate(order_items) if "rev" in item))
ordered.bypass(True)
ordered.setRenderFlag(True)
ordered.setDisplayFlag(True)

first = ch.create_material(name="parallel_first", shader="emission")
second = ch.create_material(name="parallel_second", shader="emission")
for material, z in ((first, 0.3), (second, 0.9)):
    emission = material.node("surface_shader")
    emission.parm("strength").set(1)
    field = material.createNode("cycles::attribute::1", "random_field")
    field.parm("attribute").set("rand")
    colour = material.createNode("cycles::attribute::1", "instance_colour")
    colour.parm("attribute").set("Cd")
    combine = material.createNode("cycles::combine_xyz::1", "colours")
    combine.setNamedInput("x", colour, "fac")
    combine.setNamedInput("y", field, "fac")
    combine.parm("z").set(z)
    emission.setNamedInput("color", combine, "vector")
obj.parm("shop_materialpath").set(first.path())
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=10, projection=1, orthowidth=8))
rop = menu_actions.create_rop()
device_selection.set_devices(rop, [device])
rop.setParms(
    dict(
        objectroot=scene.path(),
        vobject="*",
        lights="",
        camera=camera.path(),
        resx=width,
        resy=height,
        samples=2,
        ipr_samples=2,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_texture_cache=0,
        render_threads=2,
        world_source="color",
        curve_width=0.02,
        geometry_motion="velocity",
    )
)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))


def reset():
    hou.setFrame(1)
    fields.parm("test_offset").set(0)
    deleted.bypass(True)
    ordered.bypass(True)
    prototypes[3].parm("rows").set(6)
    obj.parm("shop_materialpath").set(first.path())


def edit(step):
    if step.startswith("frame "):
        hou.setFrame(int(step.split()[1]))
    elif step == "attribute":
        fields.parm("test_offset").set(0.25)
    elif step == "delete":
        deleted.bypass(False)
    elif step == "reorder":
        ordered.bypass(False)
    elif step == "prototype":
        prototypes[3].parm("rows").set(9)
    elif step == "material":
        obj.parm("shop_materialpath").set(second.path())
    else:
        raise AssertionError(step)


steps = [
    "frame 1",
    "frame 2",
    "frame 3",
    "attribute",
    "delete",
    "reorder",
    "frame 4",
    "prototype",
    "material",
    "frame 5",
    "frame 2",
]
counters = (
    "object_updates",
    "adapter_full_object_updates",
    "adapter_pose_updates",
    "adapter_attribute_updates",
    "adapter_pose_attribute_updates",
    "packed_instances",
    "geometry_resources",
    "mesh_variant_resources",
)


def run(serial, blur):
    if serial:
        os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
    else:
        os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    rop.parm("allowmotionblur").set(int(blur))
    reset()
    records = []
    with ch.Session(
        device=device, width=width, height=height, samples=2, threads=2, collect_diagnostics=True
    ) as session:
        for step in steps:
            edit(step)
            frame = session.wait(session.apply(native_scene.capture(rop), reuse_if_unchanged=True))
            status = session.status()
            assert not status["error"] and status["state"] == "complete", (step, status)
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"])
            assert np.isfinite(pixels).all(), step
            records.append(
                dict(
                    step=step,
                    pixels=pixels.copy(),
                    digest=hashlib.sha256(plane["pixels"]).hexdigest(),
                    coverage=int((pixels[:, :, 3] > 0.5).sum()),
                    counters={key: status.get(key) for key in counters},
                    diagnostics=json.dumps(status.get("source_diagnostics"), sort_keys=True, default=str),
                )
            )
    os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    return records


try:
    for blur in (False, True):
        serial, parallel = run(True, blur), run(False, blur)
        label = "blur" if blur else "noblur"
        report["runs"][label] = [
            dict(step=a["step"], digest=a["digest"], coverage=a["coverage"], counters=a["counters"]) for a in serial
        ]
        for a, b in zip(serial, parallel, strict=True):
            assert a["coverage"] > 1000, (label, a["step"], a["coverage"])
            assert a["counters"] == b["counters"], (label, a["step"], a["counters"], b["counters"])
            assert a["diagnostics"] == b["diagnostics"], (label, a["step"])
            difference = float(np.abs(a["pixels"] - b["pixels"]).max())
            assert a["digest"] == b["digest"], (label, a["step"], difference)
        # The edits really reach the image and the update kinds.
        by_step = {record["step"]: record for record in serial}
        assert by_step["frame 2"]["counters"]["adapter_pose_updates"] > 3000, by_step["frame 2"]["counters"]
        for step in ("attribute", "delete", "prototype", "material"):
            assert by_step[step]["digest"] != serial[steps.index(step) - 1]["digest"], (label, step)
        # Named identities: a reorder alone changes no object.
        assert by_step["reorder"]["digest"] == by_step["delete"]["digest"], label
        assert by_step["reorder"]["counters"]["adapter_full_object_updates"] == 0, by_step["reorder"]["counters"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(dict(result=report["result"], runs=report["runs"]), default=str), flush=True)
assert report["result"] == "passed", report.get("error")
