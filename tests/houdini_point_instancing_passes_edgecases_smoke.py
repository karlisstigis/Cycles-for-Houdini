# SPDX-License-Identifier: Apache-2.0
"""Point instancing edge cases (validation). Generated scenes only. Expectations fixed first.
Motion (camera shutter 0.5, 5 motion samples), each against BOTH the native Copy-to-Points packed twin
(point_instancing_common.build_twin) and a static-time average (24 motion-blur-off renders at uniform shutter times):
  transform-only, deformation-only, combined (+ animated prototype OBJ), velocity (linear motion, exact v).
Identity: duplicate ids (diagnosed, == twin), index identity with a count change inside the shutter
(population frozen: == static nominal render, diagnosed, == twin), animated prototype selection across
frames (== twin per frame, == fresh session) and inside the shutter (diagnosed, the switching instance
renders as its static nominal state, not interpolated). Overrides: point Cd/Alpha/material/dielectric
priority added then removed in one session -> bit-identical (CPU) to the never-overridden render."""

from support.paths import artifact_label

import argparse, json, os, sys, traceback
from pathlib import Path
import numpy as np
import hou

HERE = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
sys.path.insert(
    0, str(Path(__file__).resolve().parent)
)  # helpers (point_instancing_common, point_instance_oracle) live next to this test
import pass_fixtures as pf
import point_instancing_common as common
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties
from cycles_houdini.device_selection import set_devices

ap = argparse.ArgumentParser()
ap.add_argument("--device", default="cpu")
ap.add_argument("--out", default="")
ap.add_argument("--samples", type=int, default=64)
ap.add_argument("--static-samples", type=int, default=16)
ap.add_argument("--steps", type=int, default=24)
ap.add_argument("--only", default="")
a = ap.parse_args()
out = Path(a.out or ".")
out.mkdir(parents=True, exist_ok=True)
dev = common.pick_device(a.device)
W, H, S = 128, 96, a.samples
exact = dev["backend"].upper() == "CPU"
report = dict(test="edgecases", device=dev, host=os.environ.get("CYCLES_HOUDINI_HOST_ROOT"), checks=[], failures=[])


def check(name, ok, **d):
    report["checks"].append(dict(name=name, ok=bool(ok), **d))
    print(("ok: " if ok else "FAIL: ") + name, json.dumps(d, default=str)[:700], flush=True)
    if not ok:
        report["failures"].append(name)


def box3(img):
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    return sum(p[i : i + img.shape[0], j : j + img.shape[1]] for i in range(3) for j in range(3)) / 9


def close(x, y, tol=0.015):
    bx, by = box3(x[:, :, :3]), box3(y[:, :, :3])
    cov = (bx.sum(2) > 1e-3) | (by.sum(2) > 1e-3)
    m = float(np.abs(bx - by)[cov].mean()) if cov.any() else 0.0
    e = float(x[:, :, :3].sum() / max(y[:, :, :3].sum(), 1e-9))
    return dict(
        mean_abs_box3_covered=m,
        energy_ratio=e,
        covered=int(cov.sum()),
        passed=bool(cov.sum() > 100 and m < tol and abs(e - 1) < 0.03),
    )


hou.setFps(24)
NOMINAL = 10
hou.setFrame(NOMINAL)
root = hou.node("/obj")
mat = common.material("lit", "emission", (0.9, 0.8, 0.3), 1)
mat2 = common.material("lit2", "emission", (0.2, 0.6, 0.9), 1)
cdm = common.attribute_material("cdm", "Cd", "Alpha")
proto, box = common.geo_obj("proto", "box", size=(0.5, 0.25, 0.25))
proto.parm("shop_materialpath").set(mat.path())
proto.setDisplayFlag(False)
proto2, _ = common.geo_obj("proto2", "sphere", type=1, rad=(0.2, 0.2, 0.2))
proto2.parm("shop_materialpath").set(mat2.path())
proto2.setDisplayFlag(False)
inst = root.createNode("instance", "inst")
for c in inst.children():
    c.destroy()
pts = common.wrangle_points(inst, "", extra_int_parms=(("count", 5),))
inst.parm("instancepath").set("../proto")
inst.parm("ptinstance").set("fast")
inst.parm("ptmotionblur").set("deform")
twin = common.build_twin("twin", inst, [proto, proto2])
object_properties.install(twin)
object_properties.install(inst)
cam = root.createNode("cam", "cam")
pf.set_parameters(cam, dict(tz=6, resx=W, resy=H))
cam.parm("projection").set("ortho")
cam.parm("orthowidth").set(6)
cam.parm("shutter").set(0.5)
rop = hou.node("/out").createNode("cycles_ipr", "edge_rop")
set_devices(rop, dev["id"])
pf.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot="/obj",
        camera=cam.path(),
        resx=W,
        resy=H,
        samples=S,
        ipr_samples=S,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=1,
        allowmotionblur=1,
        motion_samples=5,
        geometry_motion="deformation",
        seed=4,
        animate_seed=0,
    ),
)
if rop.parmTuple("background"):
    rop.parmTuple("background").set((0, 0, 0))


def points(body, ids=True):
    pts.parm("snippet").set(
        """float t = @Time; int n = chi("count");
for (int i = 0; i < n; i++) { %s }"""
        % body
    )


LINEAR = """vector P0 = set(-2.0 + i * 1.0, 0.8 * ((i %% 2) * 2 - 1) * 0.5, 0); vector vel = set(3.0, 1.0 * ((i %% 3) - 1), 0) * %s;
int p = addpoint(0, P0 + vel * (t - %r)); %s setpointattrib(0, "v", p, vel);
setpointattrib(0, "orient", p, quaternion(radians(%s * (t - %r) * 360.0 + i * 25), {0, 0, 1}));"""


def linear(speed="1", spin="0.0", ids='setpointattrib(0, "id", p, i);', extra=""):
    return LINEAR % (speed, hou.frameToTime(NOMINAL), ids, spin, hou.frameToTime(NOMINAL)) + extra


def render(session, objects, label, blur=True, at=None):
    rop.parm("allowmotionblur").set(1 if blur else 0)
    if at is None:
        hou.setFrame(NOMINAL)
    else:
        hou.setTime(at)
    rop.parm("vobject").set(objects)
    upd = native_scene.capture(rop)
    fr = session.wait(session.apply(upd))
    st = session.status()
    img = common.plane(fr, "combined")
    np.save(out / (label + ".npy"), img)
    hou.setFrame(NOMINAL)
    return img, [d["code"] for d in st.get("source_diagnostics", [])], st


def static_average(session, objects, label):
    rop.parm("allowmotionblur").set(1)
    hou.setFrame(NOMINAL)
    motion = native_scene.motion_configuration(rop, hou.time())
    times = native_scene.motion_times(motion, hou.time())
    if len(times) < 2:
        raise RuntimeError("no shutter interval: %r" % (times,))
    t0, t1 = min(times), max(times)
    n = a.steps
    pf.set_parameters(rop, dict(samples=a.static_samples, ipr_samples=a.static_samples))
    acc = None
    for k in range(n):
        img, _, _ = render(session, objects, label + "_static%02d" % k, blur=False, at=t0 + (k + 0.5) * (t1 - t0) / n)
        acc = img if acc is None else acc + img
    pf.set_parameters(rop, dict(samples=S, ipr_samples=S))
    report.setdefault("shutter", {})[label] = [t0, t1, times]
    return acc / n


def case(session, label, body, motion_mode="deform", twin_motion="inherit", expect_codes=(), static_ref=True):
    points(body)
    inst.parm("ptmotionblur").set(motion_mode)
    twin.parm("cycles_geometry_motion").set(twin_motion)
    A, codes, st = render(session, inst.path(), label + "_inst")
    B, tcodes, _ = render(session, twin.path(), label + "_twin")
    still, _, _ = render(session, inst.path(), label + "_nominal", blur=False)
    r = close(A, B)
    check("%s: instancer == packed twin" % label, r["passed"], **r)
    if expect_codes is None:
        check(
            "%s: diagnostics (recorded; same as twin)" % label,
            sorted(set(codes)) == sorted(set(tcodes)) and bool(codes),
            codes=codes,
            twin_codes=tcodes,
        )
    else:
        check(
            "%s: diagnostics as expected %s" % (label, list(expect_codes)),
            set(expect_codes) <= set(codes)
            and (bool(expect_codes) or not [c for c in codes if c.startswith(("instancing", "motion"))]),
            codes=codes,
            twin_codes=tcodes,
        )
    if static_ref:
        avg = static_average(session, inst.path(), label)
        r = close(A, avg)
        check("%s: instancer blur == static-time average" % label, r["passed"], **r)
        r2 = close(A, still)
        check("%s: blur is visible (differs from nominal still)" % label, r2["mean_abs_box3_covered"] > 0.004, **r2)
    return A, B, still, codes


try:
    with ch.Session(
        device=dev["id"], width=W, height=H, samples=S, collect_diagnostics=True, use_texture_cache=False
    ) as session:
        only = set(a.only.split(",")) if a.only else None
        run = lambda k: only is None or k in only
        if run("transform"):
            case(session, "transform", linear(speed="8", spin="4"))
        if run("deformation"):
            box.parm("sizex").setExpression("0.45+0.35*sin($T*5000)")
            case(session, "deformation", linear(speed="0"))
        if run("combined"):
            box.parm("sizex").setExpression("0.45+0.35*sin($T*5000)")
            proto.parm("rz").setExpression("$T*240")
            case(session, "combined", linear(speed="8", spin="4"))
            proto.parm("rz").deleteAllKeyframes()
            proto.parm("rz").set(0)
        box.parm("sizex").deleteAllKeyframes()
        box.parm("sizex").set(0.5)
        if run("velocity"):
            case(session, "velocity", linear(speed="8"), motion_mode="velocity", twin_motion="velocity")
        if run("duplicate_ids"):
            case(
                session,
                "duplicate_ids",
                linear(speed="8", ids='setpointattrib(0, "id", p, i == 3 ? 1 : i);'),
                expect_codes=None,
                static_ref=False,
            )
            report["duplicate_ids_codes"] = report["checks"][-1].get("codes")
        if run("index_count_change"):
            pts.parm("count").setExpression("5 + ($T > %r)" % hou.frameToTime(NOMINAL))
            A, B, still, codes = case(
                session, "index_count_change", linear(ids=""), expect_codes=None, static_ref=False
            )
            r = close(A, still, tol=0.01)
            check("index_count_change: population frozen at the nominal state (== motion-off render)", r["passed"], **r)
            check("index_count_change: diagnosed", any(c.startswith("motion.") for c in codes), codes=codes)
            pts.parm("count").deleteAllKeyframes()
            pts.parm("count").set(5)
        if run("prototype_switch"):
            body = linear(
                spin="0.3",
                extra='setpointattrib(0, "instance", p, (i == 0 && t >= %r) ? "../proto2" : "../proto");'
                % hou.frameToTime(NOMINAL + 0.1),
            )
            points(body)
            inst.parm("ptmotionblur").set("deform")
            twin.parm("cycles_geometry_motion").set("inherit")
            A, codes, _ = render(session, inst.path(), "switch_inst")
            still, _, _ = render(session, inst.path(), "switch_nominal", blur=False)
            check(
                "prototype switch inside the shutter is diagnosed", "instancing.prototype_changed" in codes, codes=codes
            )
            region = (slice(None), slice(0, W // 4))  # point 0 lives in the left quarter
            r = close(A[region], still[region], tol=0.01)
            check(
                "the switching instance renders its nominal static state (not interpolated, not dropped)",
                r["passed"],
                **r,
            )
            # across frames, no blur: per-frame == twin and == fresh session
            for f in (NOMINAL, NOMINAL + 1):
                rop.parm("allowmotionblur").set(0)
                hou.setFrame(f)
                rop.parm("vobject").set(inst.path())
                x = common.plane(session.wait(session.apply(native_scene.capture(rop))), "combined")
                rop.parm("vobject").set(twin.path())
                y = common.plane(session.wait(session.apply(native_scene.capture(rop))), "combined")
                with ch.Session(device=dev["id"], width=W, height=H, samples=S, use_texture_cache=False) as fresh:
                    rop.parm("vobject").set(inst.path())
                    z = common.plane(fresh.wait(fresh.apply(native_scene.capture(rop))), "combined")
                r = close(x, y)
                check("frame %d: animated prototype selection == twin" % f, r["passed"], **r)
                c = common.image_compare(x, z)
                check(
                    "frame %d: == fresh session (no carry-over)" % f,
                    c["identical"] if exact else c["mean_abs_covered"] < 0.005,
                    **c,
                )
            hou.setFrame(NOMINAL)
        if run("override_removal"):
            rop.parm("allowmotionblur").set(0)
            rop.parm("nested_dielectrics").set(1) if rop.parm("nested_dielectrics") else None
            inst.parm("cycles_dielectric_priority").set(2) if inst.parm("cycles_dielectric_priority") else None
            base = linear(speed="0")
            over = (
                base
                + 'setpointattrib(0, "Cd", p, set(0.1 * i, 0.9, 0.3)); setpointattrib(0, "Alpha", p, 0.5); setpointattrib(0, "cycles_dielectric_priority", p, 5 + i); if (i == 2) setpointattrib(0, "shop_materialpath", p, "%s"); if (i == 3) setpointattrib(0, "shop_materialpath", p, "%s");'
                % (cdm.path(), mat2.path())
            )
            points(base)
            x0, _, s0 = render(session, inst.path(), "override_base", blur=False)
            points(over)
            x1, _, s1 = render(session, inst.path(), "override_on", blur=False)
            points(base)
            x2, _, s2 = render(session, inst.path(), "override_removed", blur=False)
            with ch.Session(device=dev["id"], width=W, height=H, samples=S, use_texture_cache=False) as fresh:
                x3 = common.plane(fresh.wait(fresh.apply(native_scene.capture(rop))), "combined")
            pr = lambda s: (s.get("dielectric_priority_min"), s.get("dielectric_priority_max"))
            check(
                "overrides change the image", float(np.abs(x1 - x0).mean()) > 1e-3, mean=float(np.abs(x1 - x0).mean())
            )
            c = common.image_compare(x2, x0)
            check(
                "removing overrides restores the inherited render (same session)",
                c["identical"] if exact else c["mean_abs_covered"] < 0.005,
                **c,
            )
            c = common.image_compare(x2, x3)
            check("removed state == fresh session", c["identical"] if exact else c["mean_abs_covered"] < 0.005, **c)
            check(
                "dielectric priority returns to the instancer value",
                pr(s2) == pr(s0) and pr(s1) != pr(s0),
                base=pr(s0),
                on=pr(s1),
                removed=pr(s2),
            )
    report["result"] = "passed" if not report["failures"] else "failed"
except Exception:
    report["result"] = "error"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
(out / ("edgecases-%s.json" % artifact_label(a.device))).write_text(json.dumps(report, indent=1, default=str))
print("RESULT", report["result"], report["failures"], flush=True)
