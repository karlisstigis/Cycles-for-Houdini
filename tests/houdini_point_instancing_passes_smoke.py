# SPDX-License-Identifier: Apache-2.0
"""Point instancing passes vs an independently built packed twin (validation).

Generated scene only. One Instance OBJ (offset/rotated/nonuniformly scaled instancer) with 12 points:
three mesh prototypes (one with a negatively scaled prototype OBJ, per-face materials and an OBJ material;
one without a material; one whose material reads instance Cd and Alpha), the Instance Object fallback
(empty s@instance), relative/absolute/op: paths, negative scale and negative pscale, pivot/trans/rot,
and a nested Instance OBJ prototype. The twin is built with native SOPs (point_instancing_common.build_twin).
Expectations fixed before rendering:
  * transforms: exporter table == prototype.world . (Houdini packedfulltransform of the twin) (rel 1e-5);
  * combined RGB/alpha, normal, depth, position, diffuse colour: twin-equal within noise;
  * object index: exactly the INSTANCER's pass index (7) on every instance pixel (not the prototype 5 /
    nested instancer 3), identical to the twin;
  * Cryptomatte material: same names and mattes as the twin; Cryptomatte object: one name per instance,
    same per-instance suffix set as the twin, per-name mattes equal to the twin;
  * identity across frames: frame 2 reverses point order (same ids, same placement): every per-instance
    name and matte equals frame 1; frame 3 moves all points: same names, mattes follow (== twin at frame 3).
"""

from support.paths import artifact_label

import argparse, json, os, sys, time, traceback
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
from cycles_houdini import native_scene, object_properties, _native
from cycles_houdini.device_selection import set_devices

ap = argparse.ArgumentParser()
ap.add_argument("--device", default="cpu")
ap.add_argument("--out", default="")
ap.add_argument("--samples", type=int, default=16)
ap.add_argument("--res", default="200x150")
ap.add_argument("--compare-with", default="")
a = ap.parse_args()
out = Path(a.out or ".")
out.mkdir(parents=True, exist_ok=True)
W, H = (int(v) for v in a.res.split("x"))
S = a.samples
dev = common.pick_device(a.device)
report = dict(test="passes", device=dev, host=os.environ.get("CYCLES_HOUDINI_HOST_ROOT"), checks=[], failures=[])


def check(name, ok, **d):
    report["checks"].append(dict(name=name, ok=bool(ok), **d))
    print(("ok: " if ok else "FAIL: ") + name, json.dumps(d, default=str)[:700], flush=True)
    if not ok:
        report["failures"].append(name)


try:
    hou.setFps(24)
    hou.setFrame(1)
    root = hou.node("/obj")
    red = common.material("red", "diffuse_bsdf", (0.8, 0.15, 0.1))
    yellow = common.material("yellow", "diffuse_bsdf", (0.9, 0.8, 0.1))
    green = common.material("green", "diffuse_bsdf", (0.1, 0.8, 0.2))
    grey = common.material("grey", "diffuse_bsdf", (0.5, 0.5, 0.5))
    attr = common.attribute_material("cd_alpha", "Cd", "Alpha", shader="diffuse_bsdf")
    protoA, boxA = common.geo_obj("protoA", "box", size=(0.5, 0.3, 0.25))
    faces = protoA.createNode("attribwrangle", "faces")
    faces.setInput(0, boxA)
    faces.parm("class").set(1)
    faces.parm("snippet").set('if (@primnum < 2) s@shop_materialpath = "%s";' % yellow.path())
    faces.setRenderFlag(True)
    faces.setDisplayFlag(True)
    protoA.parmTuple("t").set((0, 0.1, 0))
    protoA.parmTuple("r").set((10, 25, 0))
    protoA.parmTuple("s").set((1, 1.4, -1))
    protoA.parm("shop_materialpath").set(red.path())
    protoB, _ = common.geo_obj("protoB", "sphere", type=1, rad=(0.25, 0.25, 0.25))
    protoC, _ = common.geo_obj("protoC", "box", size=(0.4, 0.4, 0.4))
    protoC.parm("shop_materialpath").set(attr.path())
    inner = root.createNode("instance", "inner")
    for c in inner.children():
        c.destroy()
    common.wrangle_points(
        inner,
        'for (int i = 0; i < 3; i++) { int p = addpoint(0, set(-0.25 + 0.25*i, 0.15*i, 0)); setpointattrib(0, "id", p, i); }',
    )
    inner.parm("instancepath").set("../protoB")
    inner.parm("ptinstance").set("fast")
    inner.parmTuple("i_r").set((0, 0, 30))
    inner.parmTuple("i_s").set((0.5, 0.5, 0.5))
    inst = root.createNode("instance", "inst")
    for c in inst.children():
        c.destroy()
    SNIP = """addpointattrib(0, "pscale", 1.0); addpointattrib(0, "scale", {1,1,1}); addpointattrib(0, "Alpha", 1.0);
addpointattrib(0, "Cd", {1,1,1}); addpointattrib(0, "orient", {0,0,0,1}); addpointattrib(0, "pivot", {0,0,0});
addpointattrib(0, "trans", {0,0,0}); addpointattrib(0, "rot", {0,0,0,1}); addpointattrib(0, "shop_materialpath", "");
string paths[] = {"../protoA", "/obj/protoA", "", "../protoC", "../protoC", "../inner", "op:/obj/protoB", "../protoA", "../protoC", "../protoB", "../inner", "../protoA"};
int order[]; for (int i = 0; i < 12; i++) push(order, @Frame == 2 ? 11 - i : i);
foreach (int i; order) {
  vector pos = set((i %% 4) - 1.5, 1.0 - floor(i / 4), 0) + (@Frame == 3 ? set(0.3, -0.1, 0) : set(0, 0, 0));
  int p = addpoint(0, pos);
  setpointattrib(0, "id", p, 100 + i); setpointattrib(0, "instance", p, paths[i]);
  setpointattrib(0, "orient", p, quaternion(radians(17.0 * i), normalize(set(0.3, 1, 0.2 * i))));
  setpointattrib(0, "Cd", p, set(0.2 + 0.06 * i, 0.4, 0.9 - 0.07 * i));
  if (i == 1) { setpointattrib(0, "scale", p, {1, -1.3, 0.8}); setpointattrib(0, "shop_materialpath", p, "%s"); }
  if (i == 3) setpointattrib(0, "Alpha", p, 0.4);
  if (i == 4) { setpointattrib(0, "pivot", p, {0.1, 0.1, 0}); setpointattrib(0, "trans", p, {0, 0.1, 0}); setpointattrib(0, "rot", p, quaternion(radians(30), {1,0,0})); }
  if (i == 5) setpointattrib(0, "pscale", p, 1.2);
  if (i == 8) setpointattrib(0, "scale", p, {1.6, 0.6, 1});
  if (i == 9) setpointattrib(0, "shop_materialpath", p, "%s");
  if (i == 10) setpointattrib(0, "scale", p, {1.5, 1, -1});
  if (i == 11) setpointattrib(0, "pscale", p, -0.8);
}""" % (green.path(), green.path())
    pts = common.wrangle_points(inst, SNIP)
    inst.parm("instancepath").set("../protoB")
    inst.parm("ptinstance").set("fast")
    inst.parmTuple("i_t").set((0.2, -0.1, 0))
    inst.parmTuple("i_r").set((0, 0, 8))
    inst.parmTuple("i_s").set((1.1, 0.9, 1.0))
    inst.parm("shop_materialpath").set(grey.path())
    for node, index in ((inst, 7), (protoA, 5), (inner, 3), (protoB, 0), (protoC, 0)):
        object_properties.install(node)
        node.parm("cycles_pass_index").set(index)
    twin_inner = common.build_twin("twin_inner", inner, [protoB])
    twin = common.build_twin(
        "twin",
        inst,
        [protoA, protoB, protoC, inner],
        grey,
        nested_sources={inner.path(): twin_inner.renderNode().path()},
    )
    object_properties.install(twin)
    twin.parm("cycles_pass_index").set(7)
    object_properties.install(twin_inner)
    twin_inner.parm("cycles_pass_index").set(3)
    check(
        "twin OBJ world equals instancer world",
        np.allclose(common.m4(twin.worldTransform()), common.m4(inst.worldTransform()), atol=1e-6),
    )
    cam = root.createNode("cam", "cam")
    pf.set_parameters(cam, dict(tz=8, resx=W, resy=H))
    cam.parm("projection").set("ortho")
    cam.parm("orthowidth").set(5.6)
    rop = hou.node("/out").createNode("cycles_ipr", "passes_rop")
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
            allowmotionblur=0,
            seed=5,
            animate_seed=0,
            transparent_background=1,
            crypto_object=True,
            crypto_material=True,
            crypto_levels=6,
            pass_object_id=1,
            pass_normal=1,
            pass_depth=1,
            pass_position=1,
            pass_diffuse_color=1,
        ),
    )
    if rop.parmTuple("background"):
        rop.parmTuple("background").set((0.8, 0.8, 0.8))

    # ---- Houdini-native transform evaluation vs the exporter's synthetic table ------------------------
    for f in (1, 2, 3):
        hou.setFrame(f)
        t = hou.time()
        diags = []
        req = native_scene.geometry_requests_for_rop(rop, inst.renderNode(), t, diagnostics=diags)[0]
        table = _native.point_instance_table(req, t)
        r = common.compare_table_to_twin(table, req, twin, t)
        check(
            "frame %d: exporter instance transforms == prototype.world . Houdini packedfulltransform (Copy to Points twin)"
            % f,
            r["worst_rel"] < 1e-5
            and not r["missing_in_twin"]
            and not r["missing_in_table"]
            and not r["slot_mismatch"]
            and r["rows"] == 12,
            **r,
        )
        if f == 1:
            check(
                "request has no diagnostics",
                not diags and not table.get("source_diagnostics"),
                diagnostics=diags,
                table_diag=table.get("source_diagnostics"),
            )
            ireq = native_scene.geometry_requests_for_rop(rop, inner.renderNode(), t, diagnostics=[])[0]
            ri = common.compare_table_to_twin(_native.point_instance_table(ireq, t), ireq, twin_inner, t)
            check("nested instancer table == its twin", ri["worst_rel"] < 1e-5 and ri["rows"] == 3, **ri)
    hou.setFrame(1)

    def render(session, objects, label, frame_no=1):
        hou.setFrame(frame_no)
        rop.parm("vobject").set(objects)
        upd = native_scene.capture(rop)
        fr = session.wait(session.apply(upd))
        st = session.status()
        md = session.output_metadata()
        np.save(out / (label + ".combined.npy"), common.plane(fr, "combined"))
        return fr, st, md

    planes = ("combined", "object_id", "normal", "depth", "position", "diffuse_color")
    with ch.Session(
        device=dev["id"], width=W, height=H, samples=S, collect_diagnostics=True, use_texture_cache=False
    ) as session:
        fi, si, mi = render(session, "inst", "inst_f1")
        check(
            "instanced render has no source diagnostics",
            not si.get("source_diagnostics"),
            diagnostics=si.get("source_diagnostics"),
        )
        report["inst_status"] = {
            k: si.get(k)
            for k in (
                "geometry_conversions",
                "object_resources",
                "geometry_resources",
                "point_instances_synthesized",
                "packed_instances",
            )
        }
        ft, stw, mt = render(session, "twin", "twin_f1")
        check(
            "twin render has no source diagnostics",
            not stw.get("source_diagnostics"),
            diagnostics=stw.get("source_diagnostics"),
        )
        report["twin_status"] = {
            k: stw.get(k)
            for k in ("geometry_conversions", "object_resources", "geometry_resources", "packed_instances")
        }
        check(
            "both renders contain every pass",
            all(p in fi["planes"] and p in ft["planes"] for p in planes),
            inst=sorted(fi["planes"]),
            twin=sorted(ft["planes"]),
        )
        A, B = common.plane(fi, "combined"), common.plane(ft, "combined")
        c = common.image_compare(A, B)
        check(
            "combined RGBA equals the packed twin",
            c["covered"] > 3000 and c["mean_abs_covered"] < 0.01 and c["frac_over_0p02"] < 0.01,
            **c,
        )
        da = np.abs(A[:, :, 3] - B[:, :, 3])
        check(
            "alpha equals the twin (incl. instance Alpha 0.4 on point 3)",
            float(da.mean()) < 0.005 and float((da > 0.05).mean()) < 0.005,
            mean=float(da.mean()),
            max=float(da.max()),
            partial_alpha_pixels=int(((A[:, :, 3] > 0.05) & (A[:, :, 3] < 0.95)).sum()),
        )
        oi, ot = common.plane(fi, "object_id")[:, :, 0], common.plane(ft, "object_id")[:, :, 0]
        vi = sorted(set(np.unique(oi).tolist()))
        vt = sorted(set(np.unique(ot).tolist()))
        check(
            "object index is the instancer pass index (7) on every instance pixel, identical to the twin",
            set(vi) <= {0.0, 7.0} and 7.0 in vi and float((oi != ot).mean()) < 0.003,
            inst_values=vi,
            twin_values=vt,
            differing=float((oi != ot).mean()),
        )
        covered = (A[:, :, 3] > 0.999) & (B[:, :, 3] > 0.999)
        for p, tol in (("normal", 0.02), ("depth", 0.02), ("position", 0.02), ("diffuse_color", 0.02)):
            x, y = common.plane(fi, p), common.plane(ft, p)
            d = np.abs(x - y)[covered]
            if d.size == 0:
                check("%s pass: opaque pixels exist" % p, False)
                continue
            check(
                "%s pass equals the twin on opaque pixels" % p,
                float(d.mean()) < tol and float((d.max(axis=-1) > 0.1).mean()) < 0.01,
                mean=float(d.mean()),
                max=float(d.max()),
                frac_over_0p1=float((d.max(axis=-1) > 0.1).mean()),
            )
        # normals of the negatively scaled instances must face the camera like the twin's (handedness flip)
        n = common.plane(fi, "normal")
        nz = n[:, :, 2][covered]
        report["normal_facing_fraction"] = float((nz > 0).mean())
        Mi, Mt = common.manifests(mi), common.manifests(mt)
        check(
            "Cryptomatte layers present",
            {"CryptoObject", "CryptoMaterial"} <= set(Mi) and {"CryptoObject", "CryptoMaterial"} <= set(Mt),
            inst=sorted(Mi),
            twin=sorted(Mt),
        )
        mat_names = sorted(set(Mi.get("CryptoMaterial", {})) | set(Mt.get("CryptoMaterial", {})))
        worst = {}
        for name in mat_names:
            d = np.abs(common.matte(fi, "CryptoMaterial", name) - common.matte(ft, "CryptoMaterial", name))
            worst[name] = (float(d.mean()), float(d.max()))
        check(
            "CryptoMaterial names and mattes equal the twin",
            set(Mi.get("CryptoMaterial", {})) == set(Mt.get("CryptoMaterial", {}))
            and all(m < 0.003 for m, _ in worst.values()),
            inst=sorted(Mi.get("CryptoMaterial", {})),
            twin=sorted(Mt.get("CryptoMaterial", {})),
            worst=worst,
        )
        expected_mats = {red.path(), yellow.path(), green.path(), grey.path(), attr.path()}
        check(
            "material precedence: every expected material is visible",
            expected_mats <= set(Mi.get("CryptoMaterial", {})),
            expected=sorted(expected_mats),
            got=sorted(Mi.get("CryptoMaterial", {})),
        )
        pi, pt_ = inst.path() + "/packed:", twin.path() + "/packed:"
        oi_names = {n[len(pi) :]: n for n in Mi.get("CryptoObject", {}) if n.startswith(pi)}
        ot_names = {n[len(pt_) :]: n for n in Mt.get("CryptoObject", {}) if n.startswith(pt_)}
        report["crypto_object"] = dict(inst=sorted(Mi.get("CryptoObject", {})), twin=sorted(Mt.get("CryptoObject", {})))
        check(
            "CryptoObject: one name per top-level instance (12, id based)",
            len({k.split("/")[0] for k in oi_names}) == 12,
            names=sorted(oi_names),
        )
        check(
            "CryptoObject per-instance suffixes equal the twin",
            set(oi_names) == set(ot_names),
            only_inst=sorted(set(oi_names) - set(ot_names)),
            only_twin=sorted(set(ot_names) - set(oi_names)),
        )
        worst = {}
        for k in sorted(set(oi_names) & set(ot_names)):
            d = np.abs(common.matte(fi, "CryptoObject", oi_names[k]) - common.matte(ft, "CryptoObject", ot_names[k]))
            worst[k] = float(d.mean())
        check("CryptoObject per-instance mattes equal the twin", worst and max(worst.values()) < 0.003, worst=worst)
        masks1 = {k: common.matte(fi, "CryptoObject", v) for k, v in oi_names.items()}
        # frame 2: reversed point order, same ids and placement
        f2, s2, m2 = render(session, "inst", "inst_f2", 2)
        M2 = common.manifests(m2)
        n2 = {n[len(pi) :]: n for n in M2.get("CryptoObject", {}) if n.startswith(pi)}
        diff = {
            k: float(np.abs(common.matte(f2, "CryptoObject", n2[k]) - masks1[k]).max()) for k in set(n2) & set(masks1)
        }
        exact = dev["backend"].upper() == "CPU"
        check(
            "identity stable under point reordering (names and per-instance mattes unchanged)",
            set(n2) == set(oi_names) and max(diff.values()) <= (0 if exact else 0.02),
            worst=max(diff.values()) if diff else None,
            missing=sorted(set(oi_names) - set(n2)),
            new=sorted(set(n2) - set(oi_names)),
        )
        c2 = common.image_compare(common.plane(f2, "combined"), A)
        check("reordered frame renders the same image", c2["mean_abs_covered"] < (1e-7 if exact else 0.01), **c2)
        # frame 3: everything moved; names unchanged, mattes match the twin's
        f3, s3, m3 = render(session, "inst", "inst_f3", 3)
        t3, _, mt3 = render(session, "twin", "twin_f3", 3)
        M3, T3 = common.manifests(m3), common.manifests(mt3)
        n3 = {n[len(pi) :]: n for n in M3.get("CryptoObject", {}) if n.startswith(pi)}
        tn3 = {n[len(pt_) :]: n for n in T3.get("CryptoObject", {}) if n.startswith(pt_)}
        moved = {
            k: float(np.abs(common.matte(f3, "CryptoObject", n3[k]) - common.matte(t3, "CryptoObject", tn3[k])).mean())
            for k in set(n3) & set(tn3)
        }
        check(
            "moved frame: same names, mattes follow the instances (== twin)",
            set(n3) == set(oi_names)
            and moved
            and max(moved.values()) < 0.003
            and any(float(np.abs(common.matte(f3, "CryptoObject", n3[k]) - masks1[k]).max()) > 0.5 for k in n3),
            worst=max(moved.values()) if moved else None,
        )
        for name in planes:
            np.save(out / ("inst_f1.%s.npy" % name), common.plane(fi, name))
            np.save(out / ("twin_f1.%s.npy" % name), common.plane(ft, name))
    if a.compare_with:
        other = Path(a.compare_with)
        for label in ("inst_f1", "twin_f1"):
            f = other / (label + ".combined.npy")
            if f.is_file():
                report.setdefault("cross_device", {})[label] = common.image_compare(
                    np.load(f), np.load(out / (label + ".combined.npy"))
                )
    report["result"] = "passed" if not report["failures"] else "failed"
except Exception:
    report["result"] = "error"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
(out / ("passes-%s.json" % artifact_label(a.device))).write_text(json.dumps(report, indent=1, default=str))
print("RESULT", report["result"], report["failures"], flush=True)
