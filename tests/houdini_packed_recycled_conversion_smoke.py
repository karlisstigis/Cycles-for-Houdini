# SPDX-License-Identifier: Apache-2.0
"""Recycled packed conversion sends exactly what the full conversion sends.

The adapter keeps the retired generation's object table, origins and alias
sets and merges each new capture's keys into them (surviving records are
overwritten in place, dead ones erased, born ones inserted) instead of
building new tables (scene_adapter.cpp, PackedLayout). This test drives
several packed objects (moving instances with ids and instance fields, a
static packed object, nested packs, packed fragments) through frame changes
and edits: churn (a few instances die and a few are born every frame while
the rest move), only births, zero deaths, only deaths, attribute values,
attribute layout, a zero-scale instance, a renamed last instance, a reorder,
dropped names (index identities), a prototype edit, a material edit, a light
edit, everything dying and coming back, and two instances with the same name.

Every step is converted by several sessions of the same process, one after
another, from the same cooked SOP data (prototype detail identities are part
of some keys and differ between processes): the recycled path in parallel,
the recycled path serially (CYCLES_HOUDINI_SERIAL_CONVERSION=1) and the old
full path (CYCLES_HOUDINI_FULL_PACKED_CONVERSION=1). Worker payloads (sha256),
snapshot digests and update counters must be identical step for step;
packed_recycled_objects must show the recycled path where expected and never
under the full-path switch. A second sequence keeps every snapshot of the
recycled session alive: it must then refuse to recycle ("shared").
"""

from support.paths import artifacts_root
import argparse
import hashlib
import json
import os
import time
import traceback
import hou
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene
from cycles_houdini.ipr import queued, registry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
dest = artifacts_root() / "stage3/packed-recycled-conversion"
dest.mkdir(parents=True, exist_ok=True)
MOTIONS = ("off", "velocity", "acceleration", "deformation")

width, height = 120, 90

scene = hou.node("/obj").createNode("subnet", "recycled_conversion")
# Moving named instances of three prototypes with instance fields.
movers = scene.createNode("geo", "movers", run_init_scripts=False)
grid = movers.createNode("grid", "placements")
grid.setParms(dict(type=0, orient=0, rows=30, cols=30))
grid.parmTuple("size").set((6, 6))
moving = movers.createNode("attribwrangle", "moving")
moving.setInput(0, grid)
moving.parm("class").set(2)  # points
for name in ("extra", "collapse_last", "accel_on", "churn", "kill", "all_die"):
    moving.addSpareParmTuple(hou.IntParmTemplate(name, name, 1))
moving.parm("snippet").set(
    "i@id=@ptnum; float t=@Frame;"
    'int extra=chi("extra"); for(int i=0;i<extra && @ptnum==0;i++){int p=addpoint(0,set(-3.5,-3.5+0.2*i,0));'
    'setpointattrib(0,"id",p,50000+i);}'
    'if(chi("churn") && @ptnum<3){int p=addpoint(0,set(3.5,-3.5+0.2*@ptnum,0));'
    'setpointattrib(0,"id",p,100000+int(t)*10+@ptnum);}'
    'if(chi("churn") && (@ptnum*7+int(t))%101==0) removepoint(0,@ptnum);'
    'if(chi("kill") && @ptnum%50==0) removepoint(0,@ptnum);'
    'if(chi("all_die")) removepoint(0,@ptnum);'
    "@P.z+=0.05*sin(t+@ptnum*0.37); v@v=set(0.3*cos(t+@ptnum),0.2,0.05*cos(t+@ptnum*0.37));"
    'if(chi("accel_on")) v@accel=set(0,-0.5,0.1*sin(@ptnum));'
    "i@variant=@ptnum%3; f@pscale=0.04+0.01*(@ptnum%3);"
    'if(chi("collapse_last") && @ptnum==@numpt-1) f@pscale=0;'
)
prototypes = []
for index, (kind, parms) in enumerate(
    (("box", {}), ("sphere", dict(type=2, rows=6, cols=8)), ("circle", dict(type=1, divs=10)))
):
    node = movers.createNode(kind, "prototype_%d" % index)
    node.setParms(parms)
    prototypes.append(node)
merge = movers.createNode("merge", "all_copies")
for index, prototype in enumerate(prototypes):
    selected = movers.createNode("attribwrangle", "placements_%d" % index)
    selected.setInput(0, moving)
    selected.parm("class").set(2)
    selected.parm("snippet").set("if(i@variant!=%d) removepoint(0, @ptnum);" % index)
    copies = movers.createNode("copytopoints", "copies_%d" % index)
    copies.setInput(0, prototype)
    copies.setInput(1, selected)
    copies.parm("pack").set(1)
    merge.setInput(index, copies)
fields = movers.createNode("attribwrangle", "instance_fields")
fields.setInput(0, merge)
fields.parm("class").set(1)  # primitives
for name in ("rename_last", "duplicate"):
    fields.addSpareParmTuple(hou.IntParmTemplate(name, name, 1))
fields.addSpareParmTuple(hou.FloatParmTemplate("offset", "offset", 1))
fields.parm("snippet").set(
    'int id=point(0,"id",primpoint(0,@primnum,0)); s@name=sprintf("instance%d", id);'
    'if(chi("rename_last") && @primnum==@numprim-1) s@name="renamed";'
    'if(chi("duplicate") && @primnum==@numprim-1) {int first=point(0,"id",primpoint(0,0,0)); s@name=sprintf("instance%d", first);}'
    'v@Cd=set(0.2+0.6*((@primnum%11)/10.0),0.3,0.5)+chf("offset")*(@primnum%5==0);'
    "f@rand=0.1+0.8*((@primnum%17)/16.0);"
)
names = movers.createNode("attribdelete", "drop_names")
names.setInput(0, fields)
names.parm("primdel").set("name")
names.bypass(True)
layout = movers.createNode("attribdelete", "drop_rand")  # Field layout change: rand disappears.
layout.setInput(0, names)
layout.parm("primdel").set("rand")
layout.bypass(True)
ordered = movers.createNode("sort", "reordered")
ordered.setInput(0, layout)
order_items = ordered.parm("primsort").menuItems()
ordered.parm("primsort").set(next(i for i, item in enumerate(order_items) if "rev" in item))
ordered.bypass(True)
ordered.setRenderFlag(True)
ordered.setDisplayFlag(True)
# A static packed object: never moves.
still = scene.createNode("geo", "still", run_init_scripts=False)
still_points = still.createNode("grid", "points")
still_points.setParms(dict(type=0, rows=12, cols=12))
still_points.parmTuple("size").set((6, 2))
still_points.parmTuple("t").set((0, -3.5, -1))
still_box = still.createNode("box", "box")
still_box.parmTuple("size").set((0.1, 0.1, 0.1))
still_copies = still.createNode("copytopoints", "copies")
still_copies.setInput(0, still_box)
still_copies.setInput(1, still_points)
still_copies.parm("pack").set(1)
still_copies.setRenderFlag(True)
# Nested packs, moving as a whole.
nested = scene.createNode("geo", "nested", run_init_scripts=False)
nested_points = nested.createNode("grid", "points")
nested_points.setParms(dict(type=0, rows=4, cols=4))
nested_proto = nested.createNode("sphere", "ball")
nested_proto.parm("scale").set(0.2)
inner = nested.createNode("copytopoints", "inner")
inner.setInput(0, nested_proto)
inner.setInput(1, nested_points)
inner.parm("pack").set(1)
outer_points = nested.createNode("add", "outer_points")
outer_points.parm("points").set(2)
outer_points.parm("usept0").set(1)
outer_points.parm("usept1").set(1)
outer_points.parmTuple("pt0").set((2.5, 2.5, 0))
outer_points.parmTuple("pt1").set((-2.5, 2.5, 0))
outer = nested.createNode("copytopoints", "outer")
outer.setInput(0, inner)
outer.setInput(1, outer_points)
outer.parm("pack").set(1)
nested_move = nested.createNode("xform", "move")
nested_move.setInput(0, outer)
nested_move.parm("ry").setExpression("$F*7")
nested_move.setRenderFlag(True)
# Packed fragments, moving.
fragments = scene.createNode("geo", "fragments", run_init_scripts=False)
fragment_source = fragments.createNode("sphere", "source")
fragment_source.setParms(dict(type=2, rows=8, cols=10))
fragment_names = fragments.createNode("attribwrangle", "pieces")
fragment_names.setInput(0, fragment_source)
fragment_names.parm("class").set(1)
fragment_names.parm("snippet").set('s@name=sprintf("piece%d", @primnum%4);')
fragment_pack = fragments.createNode("pack", "pack")
fragment_pack.setInput(0, fragment_names)
fragment_pack.setParms(dict(packbyname=1, nameattribute="name", packedfragments=1, pivot="centroid"))
fragment_move = fragments.createNode("xform", "move")
fragment_move.setInput(0, fragment_pack)
fragment_move.parmTuple("t").set((2.5, -2.5, 0))
fragment_move.parm("rz").setExpression("$F*5")
fragment_move.setRenderFlag(True)

first = ch.create_material(name="recycled_first", shader="emission")
second = ch.create_material(name="recycled_second", shader="emission")
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
for node in (movers, still, nested, fragments):
    node.parm("shop_materialpath").set(first.path())
light = scene.createNode("hlight", "light")
light.parmTuple("t").set((0, 0, 5))
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=12, projection=1, orthowidth=9))


def make_rop():
    rop = menu_actions.create_rop()
    device_selection.set_devices(rop, [device])
    rop.setParms(
        dict(
            objectroot=scene.path(),
            vobject="*",
            camera=camera.path(),
            resx=width,
            resy=height,
            samples=1,
            ipr_samples=1,
            use_denoise=0,
            ipr_use_denoise=0,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            use_texture_cache=0,
            render_threads=2,
            world_source="color",
        )
    )
    rop.parmTuple("background").set((0, 0, 0))
    return rop


MODES = {
    "parallel": {},
    "serial": {"CYCLES_HOUDINI_SERIAL_CONVERSION": "1"},
    "full": {"CYCLES_HOUDINI_FULL_PACKED_CONVERSION": "1"},
}
ROPS = {mode: make_rop() for mode in MODES}

# Record every worker payload and the snapshot digests of each step.
RECORDS, HELD = [], []
HOLD = [False]


class Proxy:
    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        value = getattr(object.__getattribute__(self, "_inner"), name)
        if name != "encode_snapshot":
            return value

        def wrapped(desired, *a, **k):
            payload = value(desired, *a, **k)
            inner = object.__getattribute__(self, "_inner")
            RECORDS.append(dict(payload=hashlib.sha256(bytes(payload)).hexdigest(), **inner.snapshot_digest(desired)))
            if HOLD[0]:
                HELD.append(desired)
            return payload

        return wrapped


queued._native = Proxy(queued._native)


def reset():
    hou.setFrame(1)
    for parm, value in (
        (moving.parm("extra"), 0),
        (moving.parm("collapse_last"), 0),
        (moving.parm("churn"), 0),
        (moving.parm("kill"), 0),
        (moving.parm("all_die"), 0),
        (fields.parm("duplicate"), 0),
        (fields.parm("rename_last"), 0),
        (fields.parm("offset"), 0.0),
        (prototypes[1].parm("rows"), 6),
        (light.parm("light_intensity"), 1.0),
    ):
        parm.set(value)
    layout.bypass(True)
    names.bypass(True)
    ordered.bypass(True)
    movers.parm("shop_materialpath").set(first.path())


frame = [1]


def next_frame():
    frame[0] += 1
    hou.setFrame(frame[0])


# (step, expected recycled): True must recycle, False must not, None either.
STEPS = [
    ("start", False),
    ("frame", False),  # The retired generation is the empty initial scene.
    ("frame", True),
    ("frame", True),
    ("churn", True),  # Every frame from here: a few die, a few are born, the rest move.
    ("frame", True),
    ("frame", True),
    ("attribute", True),  # Values change.
    ("layout", True),  # Field layout changes: geometry variants.
    ("birth", True),  # Only births (on top of churn).
    ("calm", True),  # Churn off: births stay, zero deaths.
    ("frame", True),
    ("deaths", True),  # Only deaths.
    ("frame", True),
    ("collapse", True),  # A zero-scale instance leaves the population.
    ("rename_last", True),  # Only the last key changes.
    ("reorder", True),  # Same names, other order.
    ("prototype", True),
    ("material", True),
    ("light", True),
    ("drop_names", True),  # Index identities: every leaf key changes.
    ("frame", True),
    ("all_die", True),  # Every moving instance dies.
    ("frame", True),
    ("revive", True),
    ("frame", True),
    ("duplicate", None),  # Two instances named alike: whatever the full path does, the same.
]


def edit(step):
    if step in ("start", "frame"):
        if step == "frame":
            next_frame()
    elif step == "attribute":
        fields.parm("offset").set(0.3)
    elif step == "layout":
        layout.bypass(False)
    elif step == "churn":
        moving.parm("churn").set(1)
        next_frame()
    elif step == "birth":
        moving.parm("extra").set(3)
    elif step == "calm":
        moving.parm("churn").set(0)
    elif step == "deaths":
        moving.parm("kill").set(1)
    elif step == "all_die":
        moving.parm("all_die").set(1)
    elif step == "revive":
        moving.parm("all_die").set(0)
    elif step == "duplicate":
        names.bypass(True)
        fields.parm("duplicate").set(1)
    elif step == "collapse":
        moving.parm("collapse_last").set(1)
    elif step == "rename_last":
        fields.parm("rename_last").set(1)
    elif step == "reorder":
        ordered.bypass(False)
    elif step == "prototype":
        prototypes[1].parm("rows").set(9)
    elif step == "material":
        movers.parm("shop_materialpath").set(second.path())
    elif step == "light":
        next_frame()
        light.parm("light_intensity").set(2.0)
    elif step == "drop_names":
        names.bypass(False)
    else:
        raise AssertionError(step)


def convert(rop, mode, hold, step, expected, entries):
    for name in ("CYCLES_HOUDINI_SERIAL_CONVERSION", "CYCLES_HOUDINI_FULL_PACKED_CONVERSION"):
        os.environ.pop(name, None)
    os.environ.update(MODES[mode])
    HOLD[0] = hold
    start = len(RECORDS)
    try:
        capture = native_scene.capture(rop)
        if mode not in entries:
            entries[mode] = registry.for_rop(rop, native_scene.configuration(rop))
        entry = entries[mode]
        token = entry.apply(capture, hou.time())
        started = time.perf_counter()
        while True:
            status = entry.session.status()
            if (status.get("committed_scene_submission") or 0) >= token and (
                status.get("first_image_seconds") or 0
            ) > 0:
                break
            if status.get("state") in ("failed", "error") or status.get("error"):
                break
            assert time.perf_counter() - started < 120, (step, mode, status)
            time.sleep(0.02)
    finally:
        HOLD[0] = False
        for name in ("CYCLES_HOUDINI_SERIAL_CONVERSION", "CYCLES_HOUDINI_FULL_PACKED_CONVERSION"):
            os.environ.pop(name, None)
    return dict(
        step=step,
        expected=expected,
        records=RECORDS[start:],
        recycled=status.get("packed_recycled_objects"),
        fallback=status.get("packed_recycle_fallback"),
        packed=status.get("packed_instances"),
        poses=status.get("adapter_pose_updates"),
        full=status.get("adapter_full_object_updates"),
        attributes=status.get("adapter_attribute_updates"),
        error=str(status.get("error") or ""),
    )


def sequence(motion, modes, hold=()):
    # Every step: edit once, then convert the same cooked state in each session.
    for rop in ROPS.values():
        rop.parm("allowmotionblur").set(int(motion != "off"))
        if motion != "off":
            rop.parm("geometry_motion").set(motion)
    moving.parm("accel_on").set(int(motion == "acceleration"))
    frame[0] = 1
    reset()
    rows = {mode: [] for mode in modes}
    entries = {}
    try:
        for step, expected in STEPS:
            edit(step)
            for mode in modes:
                rows[mode].append(convert(ROPS[mode], mode, mode in hold, step, expected, entries))
    finally:
        registry.close_all()
        HELD.clear()
    return rows


def compare(label, new, others):
    assert not new["error"] or (new["step"] == "duplicate" and "Duplicate native object identity" in new["error"]), (
        label,
        "Unexpected capture failure",
        new["error"],
    )

    def records(row):
        # A session's first payload also carries its own ROP's identity (each
        # mode has its own ROP), so the first step compares the digests only.
        if label[1] == 0:
            return [{k: v for k, v in record.items() if k != "payload"} for record in row["records"]]
        return row["records"]

    for other in others:
        assert other["step"] == new["step"], (label, other["step"])
        assert records(other) == records(new), (label, new["records"], other["records"])
        for key in ("packed", "poses", "full", "attributes", "error"):
            assert other[key] == new[key], (label, key, new[key], other[key])


report = dict(result="running", device=device, runs={})
try:
    for node in (moving, fields, ordered, still_copies, nested_move, fragment_move):
        geometry = node.geometry()
        count = geometry.intrinsicValue("primitivecount") if geometry else None
        report.setdefault("nodes", []).append((node.path(), node.errors(), node.warnings(), count))
    assert ordered.geometry().intrinsicValue("primitivecount") > 500, report["nodes"]
    for motion in MOTIONS:
        rows = sequence(motion, ("parallel", "serial", "full"))
        report["runs"][motion] = [
            (row["step"], row["recycled"], row["fallback"], row["error"][:80]) for row in rows["parallel"]
        ]
        for index, new in enumerate(rows["parallel"]):
            serial, full = rows["serial"][index], rows["full"][index]
            step, expected = new["step"], new["expected"]
            label = (motion, index, step)
            assert new["records"] or new["error"], label
            compare(label, new, (serial, full))
            # A capture that fails before conversion reports no conversion path.
            assert not full["recycled"] and (full["fallback"] == "disabled" or full["error"]), (label, full)
            assert (serial["recycled"], serial["fallback"]) == (new["recycled"], new["fallback"]), (label, new, serial)
            if motion != "deformation" and expected is not None and not new["error"]:
                assert bool(new["recycled"]) == expected, (label, new["recycled"], new["fallback"])
            if new["error"]:
                # Agreement between modes cannot make a valid capture failure
                # acceptable. compare() permits only the deliberate duplicate.
                assert new["fallback"] == "duplicate", (label, new["fallback"])
        assert rows["parallel"][3]["poses"] > 500, rows["parallel"][3]
    # Snapshots of the recycled session kept alive: it must never recycle.
    rows = sequence("off", ("parallel", "full"), hold=("parallel",))
    report["runs"]["held"] = [(row["step"], row["recycled"], row["fallback"]) for row in rows["parallel"]]
    for index, (held, full) in enumerate(zip(rows["parallel"], rows["full"], strict=True)):
        label = ("held", index, held["step"])
        compare(label, held, (full,))
        assert not held["recycled"], (label, held)
        if index >= 2 and not held["error"]:
            assert held["fallback"] == "shared", (label, held["fallback"])
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, default=str), flush=True)
assert report["result"] == "passed", report.get("error")
