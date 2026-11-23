# SPDX-License-Identifier: Apache-2.0
"""Tiny packed transforms, zero-scale visibility, nesting and shutter recovery.

Compensated prototype sizes keep the image fixed while changing only packed
scale. This distinguishes transform rejection from subpixel disappearance.
"""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--execution", default="worker")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "packed-scale"
dest.mkdir(parents=True, exist_ok=True)
hou.setTime(0)
obj = hou.node("/obj").createNode("geo", "packed_scale_fixture", run_init_scripts=False)
box = obj.createNode("box")
box.parmTuple("size").set((0.6, 0.5, 0.4))
prototype = obj.createNode("xform", "compensated_prototype")
prototype.setInput(0, box)
targets = obj.createNode("python", "targets")
for name, default in [("size", 1.0), ("hide", 0.0), ("axis", 0.0), ("animate", 0.0), ("anisotropic", 0.0)]:
    targets.addSpareParmTuple(hou.FloatParmTemplate(name, name, 1, default_value=(default,)))
targets.parm("python").set("""import hou
n=hou.pwd(); g=n.geometry()
g.addAttrib(hou.attribType.Point,'id',0)
g.addAttrib(hou.attribType.Point,'pscale',1.)
g.addAttrib(hou.attribType.Point,'scale',(1.,1.,1.))
# The third instance is always collapsed. It must never render a speck.
for i,x in enumerate((-.8,.8,0.)):
    p=g.createPoint(); p.setPosition((x,0,0)); p.setAttribValue('id',10+i)
    hidden=i==2 or n.evalParm('hide')>=2 or (i==0 and n.evalParm('hide')>=1)
    p.setAttribValue('pscale',0. if hidden else n.evalParm('size'))
    if n.evalParm('anisotropic'): p.setAttribValue('scale',(1e-8,1e-5,1e-2))
    if n.evalParm('axis') and i==0: p.setAttribValue('scale',(1.,0.,1.))
""")
timed_targets = obj.createNode("attribwrangle", "shutter_visibility")
timed_targets.setInput(0, targets)
timed_targets.parm("snippet").set('if(chi("../targets/animate") && @Time<0 && i@id==10) f@pscale=0;')
copies = obj.createNode("copytopoints")
copies.setInput(0, prototype)
copies.setInput(1, timed_targets)
copies.parm("pack").set(1)
outer = obj.createNode("pack", "nested")
outer.setInput(0, copies)
outer.parm("pivot").set("origin")
copies.setRenderFlag(True)
copies.setDisplayFlag(True)
camera = hou.node("/obj").createNode("cam", "scale_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
report = dict(result="running", device=args.device, execution=args.execution, checks=[])


def rgba(frame):
    plane = frame["planes"]["combined"]
    return np.frombuffer(plane["pixels"], dtype=np.float32).reshape(frame["height"], frame["width"], plane["channels"])


with ch.Session(
    device=args.device,
    width=128,
    height=96,
    samples=4,
    threads=2,
    execution=args.execution,
    denoising={"enabled": False},
) as session:

    def render(label, source=copies, objects=2, motion=False):
        request = ch.geometry_request(obj, source, material="surface", motion="transform", at_time=0)
        update = dict(
            geometry=[request],
            camera_path=camera.path(),
            background=(0, 0, 0),
            materials=[dict(key="surface", kind="emission", color=(0.8, 0.2, 0.1))],
            motion=dict(enabled=motion, fps=24, shutter_frames=0.5, offset=0, samples=3),
        )
        frame = session.wait(session.apply(update, at_time=0), timeout=90)
        status = session.status()
        assert not status["error"] and status["object_resources"] == objects, (label, status)
        pixels = rgba(frame)
        assert np.isfinite(pixels).all(), label
        # Independent probes: visible left/right box centres and empty origin.
        for x, visible in [(38, objects == 2), (89, objects >= 1), (64, False)]:
            assert bool(pixels[48, x, 0] > 0.5) == visible, (label, x, pixels[48, x].tolist())
        report["checks"].append(dict(name=label, objects=objects, diagnostics=status["source_diagnostics"]))
        print(label, "passed", flush=True)
        return pixels, session.native_object_keys()

    baseline, identities = render("Ordinary scale and zero-sized sibling")
    for scale in (1e-3, 1e-4, 1e-5, 1e-6, 1e-8):
        targets.parm("size").set(scale)
        prototype.parm("scale").set(1 / scale)
        image, keys = render("Packed scale " + str(scale))
        assert keys == identities
        assert np.max(np.abs(image - baseline)) < 2e-5, (scale, float(np.max(np.abs(image - baseline))))
    targets.parm("size").set(1)
    targets.parm("anisotropic").set(1)
    prototype.parm("scale").set(1)
    prototype.parmTuple("s").set((1e8, 1e5, 1e2))
    image, _ = render("Tiny nonuniform scale")
    assert np.max(np.abs(image - baseline)) < 2e-5
    targets.parm("anisotropic").set(0)
    prototype.parmTuple("s").set((1, 1, 1))
    targets.parm("size").set(1e-8)
    prototype.parm("scale").set(1e8)
    targets.parm("hide").set(1)
    render("Visible to zero transition", objects=1)
    targets.parm("hide").set(2)
    render("All instances collapsed", objects=0)
    targets.parm("hide").set(0)
    restored, keys = render("Zero to visible restores identities")
    assert keys == identities and np.max(np.abs(restored - baseline)) < 2e-5
    targets.parm("axis").set(1)
    render("One zero axis is hidden", objects=1)
    targets.parm("axis").set(0)
    render("Nested tiny and collapsed instances", source=outer)
    targets.parm("size").set(-1e-8)
    reflected, _ = render("Small negative scale remains visible")
    assert np.max(np.abs(reflected - baseline)) < 2e-5
    targets.parm("size").set(1e-5)
    prototype.parm("scale").set(1e5)
    targets.parm("animate").set(1)
    image, _ = render("Collapse at shutter sample recovers nominal pose", motion=True)
    assert np.max(np.abs(image - baseline)) < 2e-5
    assert any(d["code"] == "motion.packed_unavailable" for d in session.status()["source_diagnostics"])
    targets.parm("animate").set(0)
    render("Tiny transform motion", motion=True)
    assert not session.status()["source_diagnostics"]

    # The native point-instancer uses a separate construction path. It must
    # accept the same tiny scales rather than silently dropping all instances.
    instancer = hou.node("/obj").createNode("instance", "tiny_point_instancer")
    for node in instancer.children():
        node.destroy()
    merge = instancer.createNode("object_merge")
    merge.parm("objpath1").set(timed_targets.path())
    merge.setRenderFlag(True)
    instancer.setParms(dict(instancepath=obj.path(), ptinstance="fast"))
    prototype.setRenderFlag(True)
    copies.setRenderFlag(False)
    rop = hou.node("/out").createNode("cycles_ipr", "point_scale_rop")
    rop.parm("source_mode").set("objects")
    request = native_scene.geometry_requests_for_rop(rop, merge, 0, diagnostics=[])
    for row in request:
        row["material"] = "surface"
        row["material_mode"] = "uniform"
    frame = session.wait(
        session.apply(dict(geometry=request, camera_path=camera.path(), motion=dict(enabled=False)), at_time=0),
        timeout=90,
    )
    assert session.status()["object_resources"] == 2, session.status()
    assert np.max(np.abs(rgba(frame) - baseline)) < 2e-5
    report["checks"].append(dict(name="Native point instances retain tiny scales", objects=2))

report["result"] = "passed"
path = dest / ("houdini-packed-scale-" + args.execution + "-" + ("cpu" if args.device == "CPU" else "gpu") + ".json")
path.write_text(json.dumps(report, indent=2))
print(path)
