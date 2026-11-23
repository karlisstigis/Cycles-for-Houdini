# SPDX-License-Identifier: Apache-2.0
"""Point-instance shutter displacement vs an independently authored packed twin."""

import argparse, json, os, sys
from pathlib import Path
import numpy as np
import hou

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from sampled_override_fixture import Fixture
import pass_fixtures as pf

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
f = Fixture(a.device)
f.setup("parameter")
f.obj.parm("tx").set(0)
f.sop.parm("snippet").set("v@N=set(1,0,0);")
f.sop.setRenderFlag(True)
f.sop.setDisplayFlag(True)
f.obj.setDisplayFlag(False)
f.rop.setParms(dict(geometry_motion="transform", motion_samples=3, source_mode="objects", samples=128, ipr_samples=128))
root = hou.node("/obj")
inst = root.createNode("instance", "binding_instances")
for n in inst.children():
    n.destroy()
pts = inst.createNode("attribwrangle")
pts.parm("class").set(0)
pts.setDisplayFlag(True)
pts.setRenderFlag(True)
snippet = (
    'int p=addpoint(0,{0,0,0}); setpointattrib(0,"instance",p,"%s"); setpointattrib(0,"material_override",p,sprintf("{\'height\':%%.8f}",.5+@Time*72));'
    % f.obj.path()
)
pts.parm("snippet").set(snippet)
inst.parm("ptinstance").set("fast")
inst.parm("ptmotionblur").set("deform")
# Override only geometry motion, leaving shutter bindings independently sampled.
from cycles_houdini import object_properties

object_properties.install(inst)
inst.parm("cycles_geometry_motion").set("transform")
inst.parm("i_tx").set(-0.5)
twin = root.createNode("geo", "binding_packed_twin", run_init_scripts=False)
twin.parm("tx").set(-0.5)
merge = twin.createNode("object_merge")
merge.setParms(dict(objpath1=f.sop.path(), xformtype=0))
pack = twin.createNode("pack")
pack.setInput(0, merge)
bind = twin.createNode("attribwrangle")
bind.setInput(0, pack)
bind.parm("class").set(1)
bind.parm("snippet").set(
    's@shop_materialpath="%s";s@material_override=sprintf("{\'height\':%%.8f}",.5+@Time*72);' % f.material.path()
)
bind.setDisplayFlag(True)
bind.setRenderFlag(True)
checks = []
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))


def render(obj):
    f.rop.parm("vobject").set(obj.path())
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    return f.pixels(frame), entry.session.status()


try:
    actual, st = render(inst)
    expected, ref = render(twin)
    delta = float(np.abs(actual["combined"] - expected["combined"]).max())
    assert st["material_motion_samples"] == 3, st
    assert not st["source_diagnostics"], st["source_diagnostics"]
    assert delta < 2e-4, delta
    checks.append(
        dict(
            name="sampled point override displacement equals packed twin",
            max_abs=delta,
            samples=st["material_motion_samples"],
        )
    )

    # A different second prototype slot exercises recursive request/table ownership.
    second = root.createNode("geo", "second_binding_prototype", run_init_scripts=False)
    second.setDisplayFlag(False)
    m = second.createNode("object_merge")
    m.setParms(dict(objpath1=f.sop.path(), xformtype=0))
    m.setDisplayFlag(True)
    m.setRenderFlag(True)
    second.parm("shop_materialpath").set(f.material.path())
    pts.parm("snippet").set(snippet + ' int q=addpoint(0,{8,0,0});setpointattrib(0,"instance",q,"%s");' % second.path())
    outer = root.createNode("instance", "outer_binding_instances")
    for n in outer.children():
        n.destroy()
    op = outer.createNode("attribwrangle")
    op.parm("class").set(0)
    op.parm("snippet").set("int p=addpoint(0,{0,0,0});")
    op.setDisplayFlag(True)
    op.setRenderFlag(True)
    outer.parm("instancepath").set(inst.path())
    outer.parm("ptinstance").set("fast")
    object_properties.install(outer)
    outer.parm("cycles_geometry_motion").set("transform")
    nested, ns = render(outer)
    delta = float(np.abs(nested["combined"] - expected["combined"]).max())
    assert ns["material_motion_samples"] == 3, ns
    assert not ns["source_diagnostics"], ns["source_diagnostics"]
    assert delta < 2e-4, delta
    checks.append(
        dict(
            name="nested prototype tables retain shutter mesh bindings",
            max_abs=delta,
            samples=ns["material_motion_samples"],
        )
    )
finally:
    registry.close_all()
(out / "binding-motion-review4.json").write_text(json.dumps(dict(result="passed", checks=checks), indent=2))
print("RESULT PASS", checks, flush=True)
