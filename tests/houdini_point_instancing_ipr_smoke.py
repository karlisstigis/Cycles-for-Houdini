# SPDX-License-Identifier: Apache-2.0
"""Point instancing during active IPR accumulation, modelled on the nested-dielectrics IPR edits test.
Edits land while samples accumulate (engine current_sample asserted 0 < s < target): instancer move,
prototype move (no prototype re-conversion), prototype SOP edit, points added and removed, instance
path change, prototype deleted (diagnostic, others keep rendering) and recreated (repaired), material
edit, point Cd change, then a burst. Frames never go back in revision/samples; the settled image equals
a fresh render of the final state (CPU: bit-identical). The scene observer must subscribe to prototypes
(including newly referenced and recreated ones) without reading geometry in callbacks. Generated scene only."""

from support.paths import artifact_label

import sys, os

sys.path.insert(0, os.path.join(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"], "tests"))
import pass_fixtures as pass_fixture
import argparse, json, time, traceback
from pathlib import Path
import numpy as np
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, materials, instancing
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="cpu")
parser.add_argument("--samples", type=int, default=1024)
parser.add_argument("--res", default="256x144")
parser.add_argument("--out", default="")
args = parser.parse_args()
S = args.samples
W, H = (int(v) for v in args.res.split("x"))
out = Path(args.out or ".")
out.mkdir(parents=True, exist_ok=True)
report = dict(
    host=os.environ.get("CYCLES_HOUDINI_HOST_ROOT"),
    device=args.device,
    samples=S,
    resolution=[W, H],
    checks=[],
    edits=[],
)


def check(name, **d):
    report["checks"].append(dict(name=name, **d))
    print("ok:", name, json.dumps(d, default=str)[:300], flush=True)


devs = ch.devices()
want = args.device.lower()
pick = [
    d
    for d in devs
    if d["id"] == args.device
    or (
        d["backend"].upper() == "CPU"
        if want == "cpu"
        else d["backend"].upper() == "OPTIX" and want in d["name"].lower()
    )
]
assert len(pick) == 1, (want, devs)
device = pick[0]

hou.setFrame(1)
root = hou.node("/obj")


def material(name, shader, **values):
    builder = materials.create_material(name=name, shader=shader)
    node = builder.node("surface_shader")
    for key, value in values.items():
        parm = next(p for p in node.parmTuples() if p.name().lower() == key)
        parm.set(value if isinstance(value, tuple) else (value,))
    return builder


red = material("red", "diffuse_bsdf", color=(0.8, 0.2, 0.1))
blue = material("blue", "diffuse_bsdf", color=(0.1, 0.3, 0.9))
cdmat = materials.create_material(name="cd", shader="diffuse_bsdf")
attr = cdmat.createNode("cycles::attribute::1", "cd")
pass_fixture.parm(attr, "attribute").set("Cd")
cdmat.node("surface_shader").setNamedInput("color", attr, "color")
floor_mat = material("floor", "diffuse_bsdf", color=(0.6, 0.6, 0.6))
light_mat = material("light", "emission", color=(1, 1, 1))


def geo(name, sop, material_node=None, display=False, **parms):
    node = root.createNode("geo", name)
    for c in node.children():
        c.destroy()
    shape = node.createNode(sop)
    shape.setRenderFlag(True)
    shape.setDisplayFlag(True)
    for k, v in parms.items():
        shape.parmTuple(k).set(v) if isinstance(v, tuple) else shape.parm(k).set(v)
    if material_node:
        node.parm("shop_materialpath").set(material_node.path())
    node.setDisplayFlag(display)
    return node, shape


protoA, boxA = geo("protoA", "box", red, size=(0.3, 0.3, 0.3))
protoB, sphB = geo("protoB", "sphere", blue, type=1, rad=(0.18, 0.18, 0.18))
protoC, boxC = geo("protoC", "box", cdmat, size=(0.25, 0.4, 0.25))
floor, _ = geo("floor", "grid", floor_mat, True, size=(10, 10))
lamp, _ = geo("lamp", "grid", light_mat, True, size=(3, 3))
lamp.parmTuple("t").set((0, 4, 2))
lamp.parmTuple("r").set((120, 0, 0))
inst = root.createNode("instance", "inst")
for c in inst.children():
    c.destroy()
pts = inst.createNode("attribwrangle", "points")
pts.parm("class").set(0)
pts.setRenderFlag(True)
pts.setDisplayFlag(True)


def layout(n=12, paths=("../protoA", "../protoB", "../protoC")):
    pts.parm("snippet").set(
        """int n=%d; string paths[]={%s};
for(int i=0;i<n;i++){int p=addpoint(0,set((i%%4)-1.5,0.3,floor(i/4)-1.0)); setpointattrib(0,"id",p,i);
 setpointattrib(0,"instance",p,paths[i%%len(paths)]); setpointattrib(0,"orient",p,quaternion(radians(i*23),{0,1,0}));
 setpointattrib(0,"Cd",p,set(0.2+0.06*i,0.5,0.9-0.05*i));}"""
        % (n, ",".join('"%s"' % p for p in paths))
    )


layout()
inst.parm("instancepath").set("../protoA")
inst.parm("ptinstance").set("fast")
inst.parmTuple("i_t").set((0, 0, 0))
camera = root.createNode("cam", "cam")
pass_fixture.set_parameters(camera, dict(tx=0, ty=2.6, tz=5.2, rx=-24, resx=W, resy=H))
rop = hou.node("/out").createNode("cycles_ipr", "rop")
set_devices(rop, device["id"])
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot="/obj",
        camera=camera.path(),
        resx=W,
        resy=H,
        samples=S,
        ipr_samples=S,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        use_material_assignments=1,
    ),
)
rop.parmTuple("background").set((0.05, 0.05, 0.07))

entry = None
trace = []
last_seq = 0


def session():
    return entry.session


def poll():
    global last_seq
    info = session().frame_info(last_seq)
    if info:
        last_seq = info["sequence"]
        trace.append(
            dict(
                seq=info["sequence"],
                rev=info["render_revision"],
                gen=info["generation"],
                samples=info["samples"],
                t=time.monotonic(),
            )
        )
    return session().status()


def revision(st):
    return st.get("render_revision", st["generation"])


def open_entry():
    global entry, last_seq
    registry.close_all()
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    last_seq = 0


def submit():
    return entry.apply(native_scene.capture(rop), hou.time())


def settle(generation, timeout=900):
    t0 = time.monotonic()
    while True:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        f = session().frame() if trace and trace[-1]["samples"] >= S else None
        if (
            f
            and f["generation"] == generation
            and f["complete"]
            and f["samples"] == S
            and not st.get("pending_update")
            and not st.get("capture_pending")
        ):
            return f, st
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("settle")
        time.sleep(0.005)


def wait_engine_accumulating(rev, timeout=300):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        if revision(st) >= rev and not st.get("pending_update"):
            if st["state"] == "rendering" and 1 <= st.get("current_sample", 0) < S:
                return st
            if st["state"] == "complete":
                return st
        time.sleep(0.002)
    raise TimeoutError("no accumulation for revision >= %d" % rev)


def plane(frame):
    v = frame["planes"]["combined"]
    return np.frombuffer(v["pixels"], np.float32).reshape(frame["height"], frame["width"], v["channels"]).copy()


def edit(label, action, expect=None):
    st = poll()
    before = revision(st)
    conversions = st.get("geometry_conversions")
    acc = wait_engine_accumulating(before)
    at = acc.get("current_sample")
    mid = acc["state"] == "rendering" and at is not None and 1 <= at < S
    action()
    gen = submit()
    t0 = time.monotonic()
    reset = None
    while True:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        if revision(st) > before and reset is None and st.get("current_sample", S) < (at if mid else S):
            reset = st.get("current_sample")
        if revision(st) > before and reset is not None:
            break
        if time.monotonic() - t0 > 180:
            raise TimeoutError(label)
        time.sleep(0.002)
    record = dict(
        label=label,
        generation=gen,
        revision_before=before,
        revision_after=revision(st),
        engine_samples_at_edit=at,
        edited_mid_accumulation=mid,
        engine_samples_after_restart=reset,
        conversions_before=conversions,
        conversions_after=st.get("geometry_conversions"),
        diagnostics=[d["code"] for d in st.get("source_diagnostics", [])],
    )
    report["edits"].append(record)
    print("edit:", json.dumps(record), flush=True)
    assert mid, ("edit did not land during accumulation", record)
    if expect:
        expect(st, record)
    return gen


def verify_trace(label):
    bad = []
    for a, b in zip(trace, trace[1:]):
        if b["rev"] < a["rev"]:
            bad.append(("revision went back", a, b))
        elif b["rev"] == a["rev"] and b["samples"] < a["samples"]:
            bad.append(("samples went back", a, b))
        if b["gen"] < a["gen"]:
            bad.append(("generation went back", a, b))
    assert not bad, bad[:3]
    check(label + ": frame trace monotonic", frames=len(trace))


try:
    # --- observer subscriptions (no rendering) -----------------------------------------
    calls = []
    native_scene.capture(rop)  # Records the prototypes this capture referenced.
    observer = native_scene.SceneObserver(
        rop, lambda: calls.append("scene"), object_pose_callback=lambda: calls.append("pose")
    )
    assert {n.path() for n in observer.instancing_prototypes} == {protoA.path(), protoB.path(), protoC.path()}, (
        observer.instancing_prototypes
    )
    calls.clear()
    boxA.parm("sizex").set(0.32)
    assert "scene" in calls, calls
    calls.clear()
    protoA.parm("tx").set(0.01)
    assert calls and calls[-1] == "scene", ("prototype move must take a capture, not a pose preview", calls)
    calls.clear()
    inst.parm("i_tx").set(0.01)
    assert "pose" in calls, ("instancer move is a pose edit", calls)
    protoD, _ = geo("protoD", "tube", red, rad=(0.1, 0.1))
    layout(paths=("../protoA", "../protoD", "../protoC"))
    native_scene.capture(rop)
    observer.refresh_material_bindings(None)
    assert protoD.path() in {n.path() for n in observer.instancing_prototypes}, "newly referenced prototype is observed"
    protoD.destroy()
    native_scene.capture(rop)
    observer.refresh_material_bindings(None)
    calls.clear()
    protoD, _ = geo("protoD", "tube", red, rad=(0.1, 0.1))
    assert calls, "recreating a missing prototype notifies"
    observer.close()
    layout()
    protoD.destroy()
    inst.parm("i_tx").set(0)
    protoA.parm("tx").set(0)
    boxA.parm("sizex").set(0.3)
    check("observer: prototypes, SOP edits, prototype vs instancer moves, new and recreated prototypes")

    # --- IPR edits during accumulation --------------------------------------------------
    empty_prototype = root.createNode("geo", "empty_prototype", run_init_scripts=False)
    empty_prototype.setDisplayFlag(False)

    def repair_empty_prototype():
        shape = empty_prototype.createNode("box")
        shape.setDisplayFlag(True)
        shape.setRenderFlag(True)
        empty_prototype.parm("shop_materialpath").set(red.path())

    open_entry()
    submit()
    st = wait_engine_accumulating(1)
    check("initial instanced IPR accumulating", revision=revision(st), engine_samples=st.get("current_sample"))

    def no_reconversion(st, record):
        assert record["conversions_after"] == record["conversions_before"], (
            "transform-only edit re-converted geometry",
            record,
        )

    def diagnosed(code):
        def expect(st, record):
            assert code in record["diagnostics"], (code, record)

        return expect

    def clean(st, record):
        assert not [c for c in record["diagnostics"] if c.startswith("instancing.")], record

    steps = [
        ("Instancer move", lambda: inst.parmTuple("i_t").set((0.3, 0, -0.2)), no_reconversion),
        ("Prototype move (transform only)", lambda: protoB.parmTuple("t").set((0, 0.15, 0)), no_reconversion),
        ("Prototype SOP edit", lambda: boxA.parmTuple("size").set((0.36, 0.2, 0.36)), None),
        ("Points added", lambda: layout(16), None),
        ("Points removed", lambda: layout(9), None),
        ("Instance path change", lambda: layout(9, ("../protoB", "../protoC")), None),
        ("Prototype deleted", lambda: protoB.destroy(), diagnosed("instancing.unavailable_prototype")),
        ("Prototype recreated", lambda: geo("protoB", "sphere", blue, type=1, rad=(0.2, 0.2, 0.2)), clean),
        ("Material edit", lambda: red.node("surface_shader").parmTuple("color").set((0.9, 0.7, 0.1)), None),
        (
            "Point Cd change",
            lambda: pts.parm("snippet").set(pts.parm("snippet").eval().replace("0.2+0.06*i", "0.9-0.04*i")),
            None,
        ),
        (
            "Existing empty prototype referenced",
            lambda: layout(9, ("../empty_prototype", "../protoC")),
            diagnosed("instancing.unavailable_prototype"),
        ),
        ("Existing empty prototype repaired during accumulation", repair_empty_prototype, clean),
    ]
    for label, action, expect in steps:
        edit(label, action, expect)
    check("paced edits each restarted mid-accumulation", edits=len(steps))
    before = revision(poll())
    acc = wait_engine_accumulating(before)
    gens = []
    for action in (
        lambda: inst.parm("i_ry").set(25),
        lambda: layout(14),
        lambda: protoC.parm("ry").set(40),
        lambda: blue.node("surface_shader").parmTuple("color").set((0.2, 0.9, 0.3)),
    ):
        action()
        gens.append(submit())
        poll()
    final, st = settle(gens[-1])
    assert revision(st) > before and final["generation"] == gens[-1]
    check("burst settled on the last generation", generations=gens)
    verify_trace("IPR edits")
    settled = plane(final)
    np.save(out / "settled.npy", settled)
    open_entry()
    trace.clear()
    fresh, fst = settle(submit())
    fresh_px = plane(fresh)
    np.save(out / "fresh.npy", fresh_px)
    diff = np.abs(settled - fresh_px)
    report["final_compare"] = dict(
        max_abs=float(diff.max()), mean_abs=float(diff.mean()), identical=bool(np.array_equal(settled, fresh_px))
    )
    if device["backend"].upper() == "CPU":
        assert report["final_compare"]["identical"], report["final_compare"]
    check("settled image equals a fresh render of the final state", **report["final_compare"])
    report["result"] = "passed"
except Exception:
    report["result"] = "failed"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
finally:
    registry.close_all()
(out / ("ipr-%s.json" % artifact_label(want))).write_text(json.dumps(report, indent=1, default=str))
print("RESULT", report["result"], flush=True)
hou.exit(0 if report["result"] == "passed" else 1)
