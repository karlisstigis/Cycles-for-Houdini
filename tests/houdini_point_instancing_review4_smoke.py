# SPDX-License-Identifier: Apache-2.0
"""Targeted review-4 regressions: effective attribute names, imports and staged repair."""

import argparse, json, os, sys
from pathlib import Path
import numpy as np
import hou

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pass_fixtures as pf
import cycles_houdini as ch
from cycles_houdini import native_scene, materials, instancing
from cycles_houdini.device_selection import set_devices

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--out", required=True)
a = p.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
want = a.device.lower()
matches = [
    d
    for d in ch.devices()
    if d["id"] == a.device
    or (
        d["backend"].upper() == "CPU"
        if want == "cpu"
        else d["backend"].upper() == "OPTIX" and want in d["name"].lower()
    )
]
assert len(matches) == 1, matches
a.device = matches[0]["id"]
checks = []


def check(name, ok, **details):
    checks.append(dict(name=name, ok=bool(ok), **details))
    print(name, checks[-1], flush=True)
    assert ok, checks[-1]


hou.setFrame(1)
root = hou.node("/obj")
mat = materials.create_material(name="override_reader", shader="emission")
pf.parm(mat.node("surface_shader"), "strength").set(1)
attr = mat.createNode("cycles::attribute::1", "reader")
pf.parm(attr, "attribute").set("Cd")
mat.node("surface_shader").setNamedInput("color", attr, "fac")
ch.promote_parameter(mat, attr, "attribute", name="read_attribute")
# Use an unrestricted promoted string. The Attribute node's suggestion menu is
# copied by promotion, and material overrides intentionally validate menu tokens.
group = mat.parmTemplateGroup()
template = group.find("read_attribute")
template.setMenuItems(())
template.setMenuLabels(())
template.setItemGeneratorScript("")
group.replace("read_attribute", template)
mat.setParmTemplateGroup(group)
proto = root.createNode("geo", "prototype", run_init_scripts=False)
proto.setDisplayFlag(False)
box = proto.createNode("box")
box.setDisplayFlag(True)
box.setRenderFlag(True)
proto.parm("shop_materialpath").set(mat.path())
inst = root.createNode("instance", "instances")
for n in inst.children():
    n.destroy()
pts = inst.createNode("attribwrangle")
pts.parm("class").set(0)
pts.setDisplayFlag(True)
pts.setRenderFlag(True)
inst.parm("ptinstance").set("fast")
base = 'int p=addpoint(0,{0,0,0}); setpointattrib(0,"instance",p,"../prototype"); setpointattrib(0,"pscale",p,0.4);'
pts.parm("snippet").set(base + " setpointattrib(0,\"material_override\",p,\"{'read_attribute':'pscale'}\");")
cam = root.createNode("cam", "camera")
cam.setParms(dict(tz=4, projection=1, orthowidth=2))
rop = hou.node("/out").createNode("cycles_ipr", "review4")
set_devices(rop, a.device)
pf.set_parameters(
    rop,
    dict(
        source_mode="objects",
        vobject="instances",
        camera=cam.path(),
        resx=96,
        resy=64,
        samples=4,
        ipr_samples=4,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        use_material_assignments=1,
        seed=3,
        animate_seed=0,
    ),
)
rop.parmTuple("background").set((0, 0, 0))


def image(session):
    update = native_scene.capture(rop)
    f = session.wait(session.apply(update))
    p = f["planes"]["combined"]
    return np.frombuffer(p["pixels"], np.float32).reshape(64, 96, p["channels"]).copy(), update


with ch.Session(device=a.device, width=96, height=64, samples=4, use_texture_cache=False) as s:
    overridden, u = image(s)
    np.save(out / "overridden.npy", overridden)
    (out / "overridden-capture.json").write_text(json.dumps(u, default=str, indent=1))
    (out / "overridden-status.json").write_text(json.dumps(s.status(), default=str, indent=1))
    keep = u["geometry"][0]["point_instancing"]["keep_attributes"]
    check("override retains transform-named attributes", set(instancing.TRIMMED) <= set(keep), kept=keep)
    pts.parm("snippet").set(base)
    mat.parm("read_attribute").set("pscale")
    reference, ru = image(s)
    np.save(out / "reference.npy", reference)
    (out / "reference-capture.json").write_text(json.dumps(ru, default=str, indent=1))
    (out / "reference-status.json").write_text(json.dumps(s.status(), default=str, indent=1))
    print("centres", overridden[32, 48].tolist(), reference[32, 48].tolist(), flush=True)
    error = float(np.abs(overridden - reference).max())
    check(
        "promoted string override reads pscale like an authored reader",
        error < 2e-5 and reference[..., 0].max() > 0.3,
        max_abs=error,
    )

    # Imported style parameters are resolved downstream, after the synthesis retention decision.
    mat.parm("read_attribute").set("Cd")
    style = out / "reader-style.json"
    style.write_text(json.dumps(dict(styles=[dict(overrides=dict(materialParameters=dict(read_attribute="pscale")))])))
    inst.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Style", 1))
    inst.parm("shop_materialstylesheet").set(json.dumps(dict(importFiles=[style.resolve().as_posix()])))
    styled, u = image(s)
    error = float(np.abs(styled - reference).max())
    check("imported stylesheet retains and reads pscale", error < 2e-5, max_abs=error)

    # A missing prototype may already exist as an empty OBJ. Its future SOP is a grandchild of /obj.
    inst.parm("shop_materialstylesheet").set("")
    pts.parm("snippet").set(base.replace("../prototype", "../empty_prototype"))
    empty = root.createNode("geo", "empty_prototype", run_init_scripts=False)
    empty.setDisplayFlag(False)
    native_scene.capture(rop)
    calls = []
    observer = native_scene.SceneObserver(rop, lambda: calls.append("changed"))
    try:
        calls.clear()
        shape = empty.createNode("box")
        check("adding a SOP inside an existing invalid prototype notifies IPR", bool(calls), notifications=len(calls))
        shape.setDisplayFlag(True)
        shape.setRenderFlag(True)
        empty.parm("shop_materialpath").set(mat.path())
        mat.parm("read_attribute").set("pscale")
        repaired, _ = image(s)
        check("staged prototype repair restores image", float(np.abs(repaired - reference).max()) < 2e-5)
    finally:
        observer.close()

(out / "review4.json").write_text(json.dumps(dict(result="passed", checks=checks), indent=2))
print("RESULT PASS", flush=True)
