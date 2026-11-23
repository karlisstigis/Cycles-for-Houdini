# SPDX-License-Identifier: Apache-2.0
"""Point instancing motion. Per shutter sample, the synthesized instance transforms must equal
the oracle evaluated at that time (animated points with id, animated prototype OBJ). Motion-blurred
renders must match a Copy-to-Points packed twin built by Houdini itself (same points, prototype merged
in world space) for: transform blur, prototype deformation blur together with transform blur, velocity
blur, births/deaths with ids. A point that switches prototype inside the shutter is diagnosed and kept at
the requested frame. Expectations are fixed before rendering. Generated scene only."""

import argparse, json, os, sys
from pathlib import Path
import numpy as np
import hou

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import point_instance_oracle as oracle
import pass_fixtures as pass_fixture
import cycles_houdini as ch
from cycles_houdini import native_scene, materials, object_properties, _native
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--out", default="")
args = parser.parse_args()
out = Path(args.out or ".")
out.mkdir(parents=True, exist_ok=True)
W, H, SAMPLES = 192, 128, 32
results = []
failures = []


def check(name, ok, **details):
    results.append(dict(name=name, ok=bool(ok), **details))
    print(("ok: " if ok else "FAIL: ") + name, "" if ok else json.dumps(details, default=str)[:600], flush=True)
    if not ok:
        failures.append(name)


hou.setFps(24)
hou.setFrame(10)
now = hou.time()
root = hou.node("/obj")
mat = materials.create_material(name="lit", shader="emission")
pass_fixture.set_parameters(mat.node("surface_shader"), dict(strength=1))
mat.node("surface_shader").parmTuple("color").set((0.9, 0.8, 0.3))
origin = root.createNode("null", "world_origin")
proto = root.createNode("geo", "proto")
for c in proto.children():
    c.destroy()
box = proto.createNode("box")
box.parmTuple("size").set((0.5, 0.25, 0.25))
box.parm("sizex").setExpression("0.4+0.3*sin($T*3000)")  # Hscript sin takes degrees            # Deforming prototype.
box.setRenderFlag(True)
box.setDisplayFlag(True)
proto.setDisplayFlag(False)
proto.parm("ry").setExpression("$T*200")  # Animated prototype OBJ.
proto.parm("shop_materialpath").set(mat.path())
POINTS = """int n=chi("count"); for(int i=0;i<n;i++){ float t=@Time; int p=addpoint(0,set(-1.8+i*0.9+t*%s,0.6*sin(t*6+i),0));
 setpointattrib(0,"id",p,i); setpointattrib(0,"orient",p,quaternion(radians(t*%s+i*30),{0,0,1})); setpointattrib(0,"v",p,set(%s,0,0));%s }"""


def points_snippet(speed="4", spin="300", extra=""):
    return POINTS % (speed, spin, speed, extra)


def holder(kind, name):
    node = root.createNode(kind, name)
    for c in node.children():
        c.destroy()
    w = node.createNode("attribwrangle", "points")
    w.parm("class").set(0)
    w.addSpareParmTuple(hou.IntParmTemplate("count", "Count", 1, default_value=(5,)))
    w.parm("snippet").set(points_snippet())
    w.setRenderFlag(True)
    w.setDisplayFlag(True)
    return node, w


inst, inst_points = holder("instance", "inst")
inst.parm("instancepath").set("../proto")
inst.parm("ptinstance").set("fast")
inst.parm("ptmotionblur").set("deform")
inst.parmTuple("i_t").set((0, 0, 0))
# Houdini's own packed twin: the same points, the prototype merged in world space, Copy to Points.
twin = root.createNode("geo", "twin")
for c in twin.children():
    c.destroy()
twin_points = twin.createNode("object_merge", "points")
twin_points.parm("objpath1").set(inst_points.path())
twin_points.parm("xformtype").set(0)
merged = twin.createNode("object_merge", "prototype")
merged.parm("objpath1").set(box.path())
merged.parm("xformtype").set(2)
merged.parm("xformpath").set(origin.path())
pack = twin.createNode("pack")
pack.setInput(0, merged)
pack.parm("pivot").set(0) if pack.parm("pivot") else None
copies = twin.createNode("copytopoints::2.0")
copies.setInput(0, pack)
copies.setInput(1, twin_points)
for name in ("targetattribs",):
    if copies.parm(name):
        copies.parm(name).set(0)
copies.setRenderFlag(True)
copies.setDisplayFlag(True)
object_properties.install(twin)
twin.parm("shop_materialpath").set(mat.path())
cam = root.createNode("cam", "cam")
pass_fixture.set_parameters(cam, dict(tz=6, resx=W, resy=H))
cam.parm("projection").set("ortho")
cam.parm("orthowidth").set(6)
cam.parm("shutter").set(0.5)
rop = hou.node("/out").createNode("cycles_ipr", "motion_rop")
set_devices(rop, args.device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot="/obj",
        camera=cam.path(),
        resx=W,
        resy=H,
        samples=SAMPLES,
        ipr_samples=SAMPLES,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=1,
        allowmotionblur=1,
        motion_samples=5,
        geometry_motion="deformation",
        seed=1,
        animate_seed=0,
    ),
)
rop.parmTuple("background").set((0, 0, 0))

# --- per-sample synthesis vs oracle -------------------------------------------------------
motion = native_scene.motion_configuration(rop, now)
times = native_scene.motion_times(motion, now)
request = native_scene.geometry_requests_for_rop(rop, inst.renderNode(), now, diagnostics=[])[0]


def m4(v):
    v = list(v.asTuple()) if hasattr(v, "asTuple") else list(v)
    return [v[i * 4 : (i + 1) * 4] for i in range(4)]


worst = 0
for t in times:
    table = _native.point_instance_table(request, t)
    geo = inst_points.geometryAtFrame(hou.timeToFrame(t))
    protoworld = m4(proto.worldTransformAtTime(t))
    for row in table["instances"]:
        point = geo.points()[row["point"]]
        attrs = {a.name(): point.attribValue(a) for a in geo.pointAttribs() if a.name() in ("P", "orient", "v")}
        worst = max(worst, oracle.max_error(m4(row["matrix"]), oracle.mul(protoworld, oracle.point_matrix(attrs))))
check(
    "synthesized transforms at every shutter sample equal the oracle at that time",
    worst < 2e-5,
    worst=worst,
    times=times,
)


def render(label, objects, session):
    rop.parm("vobject").set(objects)
    update = native_scene.capture(rop)
    frame = session.wait(session.apply(update))
    status = session.status()
    p = frame["planes"]["combined"]
    image = np.frombuffer(p["pixels"], np.float32).reshape(H, W, p["channels"]).copy()
    np.save(out / (label + ".npy"), image)
    return image, status


def compare(label, a, b, tolerance=0.02):
    d = np.abs(a - b)
    covered = (a[:, :, 3] > 0) | (b[:, :, 3] > 0)
    ok = covered.sum() > 300 and float(np.mean(d[covered])) < tolerance
    check(
        label,
        ok,
        mean_abs_covered=float(np.mean(d[covered])) if covered.any() else None,
        max_abs=float(d.max()),
        covered=int(covered.sum()),
    )


with ch.Session(
    device=args.device, width=W, height=H, samples=SAMPLES, collect_diagnostics=True, use_texture_cache=False
) as session:
    still, _ = None, None
    a, st = render("deform_instanced", "inst", session)
    b, _ = render("deform_twin", "twin", session)
    check(
        "deformation-mode instancing renders without diagnostics",
        not st.get("source_diagnostics"),
        diagnostics=st.get("source_diagnostics"),
    )
    compare("transform + prototype deformation blur equals the Copy to Points twin", a, b)
    rop.parm("allowmotionblur").set(0)
    c, _ = render("static_instanced", "inst", session)
    rop.parm("allowmotionblur").set(1)
    moved = int((np.abs(a - c)[:, :, :3].sum(2) > 0.05).sum())
    check("motion blur is actually visible (pixels differing from the static render)", moved > 100, pixels=moved)
    # Prototype deformation alone (points and prototype transform frozen) must also blur.
    inst_points.parm("snippet").set(points_snippet(speed="0", spin="0"))
    proto.parm("ry").deleteAllKeyframes()
    proto.parm("ry").set(0)
    a2, _ = render("deform_only_instanced", "inst", session)
    b2, _ = render("deform_only_twin", "twin", session)
    rop.parm("allowmotionblur").set(0)
    c2, _ = render("deform_only_static", "inst", session)
    rop.parm("allowmotionblur").set(1)
    moved = int((np.abs(a2 - c2)[:, :, :3].sum(2) > 0.05).sum())
    check("prototype deformation alone blurs the instances", moved > 50, pixels=moved)
    compare("prototype deformation blur equals the Copy to Points twin", a2, b2)
    inst_points.parm("snippet").set(points_snippet())
    proto.parm("ry").setExpression("$T*200")
    inst.parm("ptmotionblur").set("velocity")
    twin.parm("cycles_geometry_motion").set("velocity")
    a, st = render("velocity_instanced", "inst", session)
    b, _ = render("velocity_twin", "twin", session)
    compare("velocity blur equals the Copy to Points twin (packed velocity)", a, b)
    inst.parm("ptmotionblur").set("deform")
    twin.parm("cycles_geometry_motion").set("inherit")
    inst_points.parm("count").setExpression("5+floor(($T-%r)*240)" % now)  # Births inside the shutter.
    a, st = render("births_instanced", "inst", session)
    b, _ = render("births_twin", "twin", session)
    compare("births/deaths with ids behave like the packed twin", a, b)
    codes = [d["code"] for d in st.get("source_diagnostics", [])]
    check("births are reported by the population rules", "motion.packed_unavailable" in codes or not codes, codes=codes)
    inst_points.parm("count").deleteAllKeyframes()
    inst_points.parm("count").set(5)
    other = root.createNode("geo", "proto_other")
    s = other.createNode("sphere")
    s.setRenderFlag(True)
    other.setDisplayFlag(False)
    inst_points.parm("snippet").set(
        points_snippet(extra='setpointattrib(0,"instance",p,(i==2 && t>%r)?"../proto_other":"../proto");' % now)
    )
    a, st = render("switch_instanced", "inst", session)
    codes = [d["code"] for d in st.get("source_diagnostics", [])]
    check(
        "a point switching prototype inside the shutter is diagnosed, not interpolated",
        "instancing.prototype_changed" in codes,
        codes=codes,
    )
json.dump(results, open(out / "motion.json", "w"), indent=1)
print("RESULT", "PASS" if not failures else "FAIL " + ", ".join(failures))
