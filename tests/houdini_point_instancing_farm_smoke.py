# SPDX-License-Identifier: Apache-2.0
"""Point instancing in final/farm rendering (validation). Generated scene only.

A 5-frame animation whose instance population (count 4..8, a death and a birth of explicit ids),
instance paths (rotate through three prototypes, one missing prototype at frame 4), point attributes
(Cd), point materials (alternate; a never-before-used material appears at frame 5), a material
parameter, a prototype SOP and the instancer transform all change per frame.
  1. In-process Render to Disk of frames 1-5 (persistent data on).
  2. The scene saved to a HIP in the validation folder, rendered by a FRESH hython through the ROP
     (scripts/render_hip.py, the farm entry point), frames 1-5.
  3. Each frame rendered again by its own fresh hython (single-frame range).
Expectations (fixed before rendering): sequence frame f == fresh single frame f (CPU: bit-identical
RGBA; GPU: within noise), Render to Disk == farm, per-frame CryptoObject instance count == expected
population, the missing-prototype diagnostic only on frame 4 (nothing carries over), and every frame ==
the Copy-to-Points packed twin rendered as its own farm sequence (within noise)."""

from support.paths import artifact_label

import argparse, json, os, subprocess, sys, time, traceback
from pathlib import Path
import numpy as np
import hou

HERE = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
sys.path.insert(
    0, str(Path(__file__).resolve().parent)
)  # helpers (point_instancing_common, point_instance_oracle) live next to this test
import pass_fixtures as pf
import point_instancing_common as common
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import background_render as bg, object_properties
from cycles_houdini.device_selection import set_devices

ap = argparse.ArgumentParser()
ap.add_argument("--device", default="cpu")
ap.add_argument("--out", default="")
ap.add_argument("--samples", type=int, default=16)
a = ap.parse_args()
out = Path(a.out or ".")
out.mkdir(parents=True, exist_ok=True)
dev = common.pick_device(a.device)
W, H, S = 160, 120, a.samples
report = dict(test="farm", device=dev, host=os.environ.get("CYCLES_HOUDINI_HOST_ROOT"), checks=[], failures=[], runs=[])


def check(name, ok, **d):
    report["checks"].append(dict(name=name, ok=bool(ok), **d))
    print(("ok: " if ok else "FAIL: ") + name, json.dumps(d, default=str)[:700], flush=True)
    if not ok:
        report["failures"].append(name)


def read_exr(path):
    img = oiio.ImageInput.open(str(path))
    if not img:
        raise RuntimeError("cannot open %s: %s" % (path, oiio.geterror()))
    spec = img.spec()
    px = np.asarray(img.read_image(format=oiio.FLOAT))
    img.close()
    meta = {p.name: str(p.value) for p in spec.extra_attribs}
    return px, list(spec.channelnames), meta


def rgba(px, names):
    return px[:, :, [names.index(c) for c in ("R", "G", "B", "A")]]


try:
    project = out / "project"
    project.mkdir(parents=True, exist_ok=True)
    hou.setFps(24)
    hou.setFrame(1)
    root = hou.node("/obj")
    m1 = common.material("m_alt", "emission", (0.1, 0.7, 0.2), 1)
    m1.node("surface_shader").parm("colorg").setExpression("0.5+0.08*$F") if m1.node("surface_shader").parm(
        "colorg"
    ) else None
    m3 = common.material("m_late", "emission", (0.9, 0.9, 0.9), 1)
    grey = common.material("m_inst", "emission", (0.45, 0.45, 0.45), 1)
    red = common.material("m_red", "emission", (0.9, 0.1, 0.1), 1)
    cdm = common.attribute_material("m_cd", "Cd")
    pA, _ = common.geo_obj("pA", "box", size=(0.4, 0.25, 0.3))
    pA.parm("shop_materialpath").set(red.path())
    pA.parmTuple("r").set((0, 0, 20))
    pB, sph = common.geo_obj("pB", "sphere", type=1)
    sph.parm("radx").setExpression("0.15+0.02*$F")
    sph.parm("rady").setExpression("0.15+0.02*$F")
    sph.parm("radz").setExpression("0.15+0.02*$F")
    pC, _ = common.geo_obj("pC", "box", size=(0.3, 0.3, 0.3))
    pC.parm("shop_materialpath").set(cdm.path())
    for p in (pA, pB, pC):
        p.setDisplayFlag(False)
    inst = root.createNode("instance", "farm_inst")
    for c in inst.children():
        c.destroy()
    snippet = """int F = int(@Frame); int n = 3 + F; string protos[] = {"../pA", "../pB", "../pC"};
addpointattrib(0, "shop_materialpath", ""); addpointattrib(0, "Cd", {1,1,1});
for (int i = 0; i < n; i++) {
  if (F == 3 && i == 1) continue;                       // death of id 1 at frame 3
  int id = (F == 3 && i == 4) ? 50 : i;                 // birth of id 50 at frame 3
  int p = addpoint(0, set(-1.6 + 0.45 * i, 0.5 * sin(i + F), 0));
  setpointattrib(0, "id", p, id);
  setpointattrib(0, "instance", p, (F == 4 && i == 2) ? "../missing_proto" : protos[(i + F) %% 3]);
  setpointattrib(0, "orient", p, quaternion(radians(20.0 * i + 15.0 * F), {0, 0, 1}));
  setpointattrib(0, "Cd", p, hsvtorgb(set(frac(0.1 * i + 0.15 * F), 0.8, 0.9)));
  if ((i + F) %% 2 == 0) setpointattrib(0, "shop_materialpath", p, "%s");
  if (F == 5 && i == 0) setpointattrib(0, "shop_materialpath", p, "%s");
}""" % (m1.path(), m3.path())
    common.wrangle_points(inst, snippet)
    inst.parm("instancepath").set("../pB")
    inst.parm("ptinstance").set("fast")
    inst.parm("shop_materialpath").set(grey.path())
    inst.parm("i_rz").setExpression("4*$F")
    twin = common.build_twin("farm_twin", inst, [pA, pB, pC], grey)
    # Expected per frame (independent of the exporter): population, missing prototype.
    expected = {}
    for f in range(1, 6):
        n = 3 + f
        ids = [(50 if (f == 3 and i == 4) else i) for i in range(n) if not (f == 3 and i == 1)]
        missing = [2] if f == 4 else []
        expected[f] = dict(
            count=len(ids) - len(missing), ids=sorted(set(ids) - set(missing)), missing_diag=bool(missing)
        )
    report["expected"] = expected
    cam = root.createNode("cam", "cam")
    pf.set_parameters(cam, dict(tz=6, resx=W, resy=H))
    cam.parm("projection").set("ortho")
    cam.parm("orthowidth").set(4.4)

    def make_rop(name, objects, picture):
        rop = hou.node("/out").createNode("cycles_ipr", name)
        set_devices(rop, dev["id"])
        pf.set_parameters(
            rop,
            dict(
                source_mode="objects",
                objectroot="/obj",
                vobject=objects,
                camera=cam.path(),
                resx=W,
                resy=H,
                samples=S,
                adaptive_sampling=0,
                use_denoise=0,
                use_material_assignments=1,
                allowmotionblur=0,
                seed=2,
                animate_seed=0,
                trange=1,
                persistent_data=1,
                transparent_background=1,
                crypto_object=True,
                crypto_material=True,
                picture=picture,
            ),
        )
        for parm in rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        rop.parmTuple("f").set((1, 5, 1))
        return rop

    rop = make_rop("farm_inst_rop", inst.path(), "$HIP/rtd/inst.$F4.exr")
    trop = make_rop("farm_twin_rop", twin.path(), "$HIP/twin/twin.$F4.exr")
    scene = project / "E_point_instancing_farm.hiplc"
    hou.hipFile.save(str(scene), save_to_recent_files=False)
    t0 = time.monotonic()
    rop.render(frame_range=(1, 5, 1))
    report["rtd_seconds"] = time.monotonic() - t0  # Render to Disk, in process
    rop.parm("picture").set("$HIP/seq/inst.$F4.exr")
    hou.hipFile.save(str(scene), save_to_recent_files=False)
    script = Path(ch.__file__).resolve().parents[2] / "scripts/render_hip.py"
    if not script.is_file():
        script = HERE / "scripts/render_hip.py"
    exe = Path(hou.getenv("HFS")) / "bin/hython.exe"

    def farm(name, rop_path, options=()):
        rep = out / (name + ".json")
        cmd = [str(exe), str(script), str(scene), "--rop", rop_path, "--report", str(rep), *options]
        t = time.monotonic()
        r = subprocess.run(cmd, cwd=str(out), env=bg._environment(), capture_output=True, text=True, timeout=600)
        (out / (name + ".stdout.log")).write_text(r.stdout, encoding="utf-8")
        (out / (name + ".stderr.log")).write_text(r.stderr, encoding="utf-8")
        value = json.loads(rep.read_text()) if rep.is_file() else None
        report["runs"].append(
            dict(name=name, exit=r.returncode, seconds=time.monotonic() - t, result=value and value.get("result"))
        )
        check(
            "farm run %s succeeded" % name,
            r.returncode == 0 and value and value.get("result") == "passed",
            exit=r.returncode,
            stderr=r.stderr[-400:],
        )
        return value

    farm("sequence", "/out/farm_inst_rop")
    for f in range(1, 6):
        farm(
            "single%d" % f,
            "/out/farm_inst_rop",
            ["--start", str(f), "--end", str(f), "--output", "$HIP/single/inst.$F4.exr"],
        )
    farm("twin_sequence", "/out/farm_twin_rop")
    exact = dev["backend"].upper() == "CPU"
    prefix = inst.path() + "/packed:"
    for f in range(1, 6):
        seq, names, meta = read_exr(project / "seq" / ("inst.%04d.exr" % f))
        one, n1, _ = read_exr(project / "single" / ("inst.%04d.exr" % f))
        rtd, n2, _ = read_exr(project / "rtd" / ("inst.%04d.exr" % f))
        tw, n3, _ = read_exr(project / "twin" / ("twin.%04d.exr" % f))
        A, B, R, T = rgba(seq, names), rgba(one, n1), rgba(rtd, n2), rgba(tw, n3)
        np.save(out / ("seq.%d.npy" % f), A)
        c = common.image_compare(A, B)
        check(
            "frame %d: farm sequence == fresh single-frame render" % f,
            c["identical"] if exact else c["mean_abs_covered"] < 0.01,
            **c,
        )
        c = common.image_compare(A, R)
        check(
            "frame %d: in-process Render to Disk == farm sequence" % f,
            c["identical"] if exact else c["mean_abs_covered"] < 0.01,
            **c,
        )
        c = common.image_compare(A, T)
        check(
            "frame %d: instancer == Copy-to-Points twin" % f,
            c["covered"] > 500 and c["mean_abs_covered"] < 0.01 and c["frac_over_0p02"] < 0.01,
            **c,
        )
        manifest = common.manifests({k: v for k, v in meta.items() if k.startswith("cryptomatte/")}).get(
            "CryptoObject", {}
        )
        inst_names = sorted(n[len(prefix) :] for n in manifest if n.startswith(prefix))
        ids_ok = inst_names == sorted("id:%d" % i for i in expected[f]["ids"])
        check(
            "frame %d: CryptoObject has one name per expected instance" % f,
            len(inst_names) == expected[f]["count"] and ids_ok,
            names=inst_names,
            expected=expected[f],
        )
        diags = json.loads(meta.get("cycles_houdini:source_diagnostics") or "[]")
        codes = sorted({d.get("code") for d in diags})
        check(
            "frame %d: diagnostics only where expected (missing prototype at frame 4)" % f,
            ("instancing.unavailable_prototype" in codes) == expected[f]["missing_diag"]
            and all(c_.startswith("instancing.unavailable") for c_ in codes),
            codes=codes,
        )
    # Materials per frame: the late material appears only at frame 5.
    late = []
    for f in range(1, 6):
        _, _, meta = read_exr(project / "seq" / ("inst.%04d.exr" % f))
        late.append(
            m3.path()
            in common.manifests({k: v for k, v in meta.items() if k.startswith("cryptomatte/")}).get(
                "CryptoMaterial", {}
            )
        )
    check(
        "a material first used at frame 5 appears only at frame 5",
        late == [False, False, False, False, True],
        per_frame=late,
    )
    report["result"] = "passed" if not report["failures"] else "failed"
except Exception:
    report["result"] = "error"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
(out / ("farm-%s.json" % artifact_label(a.device))).write_text(json.dumps(report, indent=1, default=str))
print("RESULT", report["result"], report["failures"], flush=True)
