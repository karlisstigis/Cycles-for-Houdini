# SPDX-License-Identifier: Apache-2.0
"""Point instancing performance vs an equivalent Copy-to-Points packed twin (validation).
Generated scene only. For N in --counts: an Instance OBJ with N points over 3 prototypes (box, ~1k-tri
sphere, ~1k-tri torus), and an artist-style twin (Blast by s@instance, Pack, Copy to Points 2.0, Merge),
same transforms. Per mode and N, in a fresh session: SOP cook time (excluded from capture), capture time,
apply (host-blocking) time, render-start latency (apply start -> first frame of that generation), total
time to a complete low-sample frame, geometry conversions (must equal 3 for both and not scale with N),
geometry/object resources, owned geometry bytes, device allocation (current/peak), process memory,
point-instance synthesis counters; then an unchanged re-capture (cache path). At --ipr-count (100k):
IPR accumulating, 5 transform-only edits of the instancer (authoritative capture path), timing edit ->
first frame of the new revision, conversions unchanged. Expectations: conversions == 3 for every N and
mode; instanced memory and times within a small factor of the twin; no per-instance mesh duplication
(owned geometry bytes ~constant in N apart from per-instance transforms)."""

from support.paths import artifact_label

import argparse, gc, json, math, os, sys, time, traceback
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
from cycles_houdini import native_scene, registry
from cycles_houdini.device_selection import set_devices

ap = argparse.ArgumentParser()
ap.add_argument("--device", default="4090")
ap.add_argument("--out", default="")
ap.add_argument("--counts", default="1000,100000,1000000")
ap.add_argument("--ipr-count", type=int, default=100000)
ap.add_argument("--samples", type=int, default=4)
ap.add_argument("--res", default="640x360")
ap.add_argument("--modes", default="instanced,twin")
a = ap.parse_args()
out = Path(a.out or ".")
out.mkdir(parents=True, exist_ok=True)
dev = common.pick_device(a.device)
W, H = (int(v) for v in a.res.split("x"))
S = a.samples
report = dict(
    test="perf",
    device=dev,
    host=os.environ.get("CYCLES_HOUDINI_HOST_ROOT"),
    res=[W, H],
    samples=S,
    runs=[],
    ipr=[],
    checks=[],
    failures=[],
)


def save():
    (out / ("perf-%s.json" % artifact_label(a.device))).write_text(json.dumps(report, indent=1, default=str))


def check(name, ok, **d):
    report["checks"].append(dict(name=name, ok=bool(ok), **d))
    print(("ok: " if ok else "FAIL: ") + name, json.dumps(d, default=str)[:600], flush=True)
    if not ok:
        report["failures"].append(name)


KEYS = (
    "geometry_conversions",
    "object_updates",
    "owned_geometry_bytes",
    "device_allocation_bytes",
    "device_allocation_peak_bytes",
    "cooking_seconds",
    "extraction_seconds",
    "extractions",
    "cache_hits",
    "geometry_resources",
    "object_resources",
    "packed_instances",
    "mesh_variant_bytes",
    "retained_mesh_source_bytes",
    "first_image_seconds",
    "point_instance_syntheses",
    "point_instance_cache_hits",
    "point_instances_synthesized",
    "point_instance_seconds",
)

hou.setFps(24)
hou.setFrame(1)
root = hou.node("/obj")
mat = common.material("perf_mat", "diffuse_bsdf", (0.6, 0.5, 0.4))
pA, _ = common.geo_obj("pA", "box", size=(0.05, 0.05, 0.05))
pB, _ = common.geo_obj("pB", "sphere", type=1, rad=(0.04, 0.04, 0.04), freq=12)
pC, _ = common.geo_obj("pC", "torus", rad=(0.04, 0.015), rows=24, cols=24)
for p in (pA, pB, pC):
    p.parm("shop_materialpath").set(mat.path())
    p.setDisplayFlag(False)
inst = root.createNode("instance", "perf_inst")
for c in inst.children():
    c.destroy()
grid = inst.createNode("grid", "grid")
grid.parm("rows").set(10)
grid.parm("cols").set(10)
grid.parmTuple("size").set((8, 4.5))
noprims = inst.createNode("attribwrangle", "noprims")
noprims.setInput(0, grid)
noprims.parm("class").set(1)
noprims.parm("snippet").set("removeprim(0, @primnum, 0);")
attrs = inst.createNode("attribwrangle", "attrs")
attrs.setInput(0, noprims)
attrs.parm("class").set(2)
attrs.addSpareParmTuple(hou.IntParmTemplate("count", "count", 1, default_value=(1000,)))
attrs.parm("snippet").set("""if (@ptnum >= chi("count")) { removepoint(0, @ptnum); return; }
string paths[] = {"../pA", "../pB", "../pC"}; s@instance = paths[@ptnum % 3]; i@id = @ptnum;
p@orient = quaternion(radians(rand(@ptnum) * 360), normalize(set(rand(@ptnum + 1) - .5, 1, rand(@ptnum + 2) - .5)));
f@pscale = 0.6 + 0.8 * rand(@ptnum + 3);""")
attrs.setRenderFlag(True)
attrs.setDisplayFlag(True)
inst.parm("ptinstance").set("fast")
inst.parm("instancepath").set("../pA")
# Artist-style twin: Blast by s@instance, Pack, Copy to Points, Merge (no Python per point).
twin = root.createNode("geo", "perf_twin")
for c in twin.children():
    c.destroy()
twin.setFirstInput(inst)
origin = common.origin_null()
pts = twin.createNode("object_merge", "points")
pts.parm("objpath1").set(attrs.path())
pts.parm("xformtype").set(0)
merge = twin.createNode("merge")
for k, p in enumerate((pA, pB, pC)):
    om = twin.createNode("object_merge")
    om.parm("objpath1").set(p.renderNode().path())
    om.parm("xformtype").set(2)
    om.parm("xformpath").set(origin.path())
    pack = twin.createNode("pack")
    pack.setInput(0, om)
    sel = twin.createNode("blast")
    sel.setInput(0, pts)
    sel.parm("grouptype").set("points")
    sel.parm("group").set('@instance=="../%s"' % p.name())
    sel.parm("negate").set(1)
    ctp = twin.createNode("copytopoints::2.0")
    ctp.setInput(0, pack)
    ctp.setInput(1, sel)
    merge.setNextInput(ctp)
merge.setRenderFlag(True)
merge.setDisplayFlag(True)
twin.parm("shop_materialpath").set(mat.path())
light = root.createNode("hlight::2.0", "sun") if False else None
cam = root.createNode("cam", "cam")
pf.set_parameters(cam, dict(tz=6, resx=W, resy=H))
rop = hou.node("/out").createNode("cycles_ipr", "perf_rop")
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
        allowmotionblur=0,
        use_material_assignments=1,
        seed=1,
    ),
)
if rop.parmTuple("background"):
    rop.parmTuple("background").set((0.7, 0.7, 0.75))


def set_count(n):
    side = int(math.ceil(math.sqrt(n)))
    grid.parm("rows").set(side)
    grid.parm("cols").set(side)
    attrs.parm("count").set(n)
    grid.parmTuple("size").set((8, 4.5))


def stats(st):
    return {k: st.get(k) for k in KEYS}


def measure(mode, n):
    set_count(n)
    obj = inst if mode == "instanced" else twin
    rop.parm("vobject").set(obj.path())
    t = time.perf_counter()
    obj.renderNode().cook(force=True)
    cook = time.perf_counter() - t
    gc.collect()
    mem0 = ch.process_memory()
    rec = dict(mode=mode, count=n, sop_cook_seconds=cook)
    with ch.Session(
        device=dev["id"], width=W, height=H, samples=S, collect_diagnostics=True, use_texture_cache=False
    ) as session:
        t0 = time.perf_counter()
        upd = native_scene.capture(rop)
        t1 = time.perf_counter()
        gen = session.apply(upd)
        t2 = time.perf_counter()
        first = None
        while time.perf_counter() - t2 < 600:
            info = session.frame_info()
            if info and info["generation"] == gen:
                first = time.perf_counter()
                break
            st = session.status()
            if st["state"] == "failed":
                raise RuntimeError(st["error"])
            time.sleep(0.001)
        frame = session.wait(gen)
        t3 = time.perf_counter()
        st = session.status()
        mem1 = ch.process_memory()
        rec.update(
            capture_seconds=t1 - t0,
            apply_seconds=t2 - t1,
            render_start_latency=(first - t1) if first else None,
            prep_to_first_frame=(first - t0) if first else None,
            total_seconds=t3 - t0,
            status=stats(st),
            diagnostics=[d["code"] for d in st.get("source_diagnostics", [])],
            memory_before=mem0,
            memory_after=mem1,
        )
        # Unchanged re-capture (cache/reuse path).
        t4 = time.perf_counter()
        upd2 = native_scene.capture(rop)
        t5 = time.perf_counter()
        gen2 = session.apply(upd2, reuse_if_unchanged=True)
        t6 = time.perf_counter()
        session.wait(gen2)
        st2 = session.status()
        rec.update(recapture_seconds=t5 - t4, reapply_seconds=t6 - t5, recapture_status=stats(st2))
        img = common.plane(frame, "combined")
        rec["coverage"] = float((img[:, :, :3].sum(axis=2) > 0).mean())
    gc.collect()
    rec["memory_closed"] = ch.process_memory()
    return rec


try:
    counts = [int(c) for c in a.counts.split(",")]
    # Warm-up (not recorded): the first render in a process pays device/kernel initialisation
    # (seen as a 3.6 s first image on a freshly built host), which must not be charged to either mode.
    set_count(1000)
    rop.parm("vobject").set(twin.path())
    with ch.Session(device=dev["id"], width=W, height=H, samples=S, use_texture_cache=False) as warm:
        t = time.perf_counter()
        warm.wait(warm.apply(native_scene.capture(rop)))
        report["warmup_seconds"] = time.perf_counter() - t
    for n in counts:
        for mode in a.modes.split(","):
            try:
                rec = measure(mode, n)
                report["runs"].append(rec)
                print(
                    "run:",
                    json.dumps(
                        {
                            k: rec[k]
                            for k in (
                                "mode",
                                "count",
                                "sop_cook_seconds",
                                "capture_seconds",
                                "apply_seconds",
                                "render_start_latency",
                                "total_seconds",
                            )
                        }
                    ),
                    json.dumps(
                        {
                            k: rec["status"][k]
                            for k in (
                                "geometry_conversions",
                                "owned_geometry_bytes",
                                "device_allocation_peak_bytes",
                                "object_resources",
                                "geometry_resources",
                            )
                        }
                    ),
                    flush=True,
                )
            except Exception:
                report["runs"].append(dict(mode=mode, count=n, error=traceback.format_exc()))
                print(traceback.format_exc(), flush=True)
            save()
    for n in counts:
        r = {x["mode"]: x for x in report["runs"] if x["count"] == n and "status" in x}
        if "instanced" in r:
            check(
                "N=%d instanced: 3 geometry conversions (no per-instance meshes)" % n,
                r["instanced"]["status"]["geometry_conversions"] == 3,
                conversions=r["instanced"]["status"]["geometry_conversions"],
                twin=r.get("twin", {}).get("status", {}).get("geometry_conversions"),
            )
            check(
                "N=%d instanced: no diagnostics" % n,
                not r["instanced"]["diagnostics"],
                diagnostics=r["instanced"]["diagnostics"],
            )
        if "instanced" in r and "twin" in r:
            i, t = r["instanced"]["status"], r["twin"]["status"]
            check(
                "N=%d owned geometry bytes within 1.5x of the twin" % n,
                i["owned_geometry_bytes"] <= 1.5 * max(t["owned_geometry_bytes"], 1) + 1e6,
                instanced=i["owned_geometry_bytes"],
                twin=t["owned_geometry_bytes"],
            )
            check(
                "N=%d device peak within 1.5x of the twin" % n,
                i["device_allocation_peak_bytes"] <= 1.5 * max(t["device_allocation_peak_bytes"], 1) + 16e6,
                instanced=i["device_allocation_peak_bytes"],
                twin=t["device_allocation_peak_bytes"],
            )
            check(
                "N=%d prep-to-first-frame within 2x of the twin (+0.5 s)" % n,
                r["instanced"]["prep_to_first_frame"] <= 2 * r["twin"]["prep_to_first_frame"] + 0.5,
                instanced=r["instanced"]["prep_to_first_frame"],
                twin=r["twin"]["prep_to_first_frame"],
                instanced_capture=r["instanced"]["capture_seconds"],
                twin_capture=r["twin"]["capture_seconds"],
            )
    small = [x for x in report["runs"] if x["mode"] == "instanced" and "status" in x]
    if len(small) >= 2:
        b = sorted(small, key=lambda x: x["count"])
        per_inst = (b[-1]["status"]["owned_geometry_bytes"] - b[0]["status"]["owned_geometry_bytes"]) / max(
            1, b[-1]["count"] - b[0]["count"]
        )
        report["owned_bytes_per_instance"] = per_inst
        check(
            "owned geometry grows < 512 bytes per instance (no mesh duplication; a mesh copy would be >10 kB)",
            per_inst < 512,
            bytes_per_instance=per_inst,
        )
    save()
    # ---- IPR: transform-only edits of the instancer while accumulating ---------------------------------
    for mode in a.modes.split(","):
        set_count(a.ipr_count)
        obj = inst if mode == "instanced" else twin
        rop.parm("vobject").set(obj.path())
        pf.set_parameters(rop, dict(ipr_samples=1 << 20, samples=1 << 20))
        registry.close_all()
        entry = registry.for_rop(rop, native_scene.configuration(rop))
        s = entry.session
        seq = [0]

        def poll():  # worker sessions advance when frames are read (as in D's IPR smoke)
            info = s.frame_info(seq[0])
            if info:
                seq[0] = info["sequence"]
            st = s.status()
            if st["state"] == "failed":
                raise RuntimeError(st["error"])
            return info, st

        rev = lambda x: x.get("render_revision", x.get("generation"))
        g = entry.apply(native_scene.capture(rop), hou.time())
        t0 = time.perf_counter()
        started = False
        while time.perf_counter() - t0 < 300:
            info, st = poll()
            if st["state"] == "rendering" and st.get("current_sample", 0) >= 2 and not st.get("pending_update"):
                started = True
                break
            time.sleep(0.002)
        if not started:
            raise TimeoutError("IPR did not start accumulating: %r" % s.status())
        edits = []
        for k in range(6):
            info, st = poll()
            before = rev(st)
            conv = st["geometry_conversions"]
            syn = st.get("point_instance_syntheses")
            hits = st.get("point_instance_cache_hits")
            at = st.get("current_sample")
            inst.parmTuple("i_t").set((0.05 * (k + 1), 0, 0))
            t1 = time.perf_counter()
            upd = native_scene.capture(rop)
            t2 = time.perf_counter()
            gen = entry.apply(upd, hou.time())
            t3 = time.perf_counter()
            first = None
            last_info = None
            frame_seen = None
            while time.perf_counter() - t3 < 120:
                info, st = poll()
                if info:
                    last_info = {
                        k: info.get(k) for k in ("sequence", "generation", "render_revision", "samples", "complete")
                    }
                if info and rev(info) > before and frame_seen is None:
                    frame_seen = time.perf_counter()
                if (
                    rev(st) > before
                    and st["state"] in ("rendering", "complete")
                    and st.get("current_sample", 0) >= 1
                    and not st.get("pending_update")
                ):
                    first = time.perf_counter()
                    break
                time.sleep(0.0005)
            info, st = poll()
            edits.append(
                dict(
                    edit=k,
                    samples_at_edit=at,
                    capture_seconds=t2 - t1,
                    apply_seconds=t3 - t2,
                    edit_to_status_restart=(first - t1) if first else None,
                    edit_to_first_image=(frame_seen - t1) if frame_seen else None,
                    conversions_before=conv,
                    conversions_after=st["geometry_conversions"],
                    syntheses_delta=(st.get("point_instance_syntheses") or 0) - (syn or 0),
                    cache_hits_delta=(st.get("point_instance_cache_hits") or 0) - (hits or 0),
                )
            )
            edits[-1].update(
                generation=gen,
                revision_before=before,
                last_info=last_info,
                status_after={
                    k: st.get(k) for k in ("state", "generation", "render_revision", "current_sample", "pending_update")
                },
            )
            if first is None:
                raise TimeoutError("no frame after edit %d: %r" % (k, edits[-1]))
            t4 = time.perf_counter()
            while time.perf_counter() - t4 < 0.3:
                poll()
                time.sleep(0.01)
        registry.close_all()
        pf.set_parameters(rop, dict(ipr_samples=S, samples=S))
        inst.parmTuple("i_t").set((0, 0, 0))
        e = edits[1:]  # first edit warms caches
        # Note: this measures a full native_scene.capture() per edit; interactive viewport drags use the registry's deferred
        # capture with pose previews instead, so these numbers are an upper bound for the authoritative update path.
        rec = dict(
            mode=mode,
            count=a.ipr_count,
            edits=edits,
            median_edit_to_first_image=float(
                np.median([x["edit_to_first_image"] for x in e if x["edit_to_first_image"]])
            ),
            median_edit_to_status_restart=float(
                np.median([x["edit_to_status_restart"] for x in e if x["edit_to_status_restart"]])
            ),
            median_capture=float(np.median([x["capture_seconds"] for x in e])),
            median_apply=float(np.median([x["apply_seconds"] for x in e])),
        )
        report["ipr"].append(rec)
        print("ipr:", json.dumps({k: v for k, v in rec.items() if k != "edits"}), flush=True)
        check(
            "IPR %s %d: transform-only edits re-convert no geometry" % (mode, a.ipr_count),
            all(x["conversions_after"] == x["conversions_before"] for x in edits),
            conversions=[(x["conversions_before"], x["conversions_after"]) for x in edits],
        )
        save()
    ri = {x["mode"]: x for x in report["ipr"]}
    if "instanced" in ri and "twin" in ri:
        check(
            "IPR instancer move: edit to first new-revision image within 2x of the twin (+0.2 s)",
            ri["instanced"]["median_edit_to_first_image"] <= 2 * ri["twin"]["median_edit_to_first_image"] + 0.2,
            instanced=ri["instanced"]["median_edit_to_first_image"],
            twin=ri["twin"]["median_edit_to_first_image"],
            status_restart_instanced=ri["instanced"]["median_edit_to_status_restart"],
            status_restart_twin=ri["twin"]["median_edit_to_status_restart"],
        )
    report["result"] = "passed" if not report["failures"] else "failed"
except Exception:
    report["result"] = "error"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
finally:
    registry.close_all()
save()
print("RESULT", report["result"], report["failures"], flush=True)
