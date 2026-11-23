# SPDX-License-Identifier: Apache-2.0
"""Packed instances that are all collapsed to zero scale (particles on their birth frame) show
nothing, so their material must not report a missing instance attribute: the packed points do
carry the attribute, there is only no visible instance to read it from. With visible instances
the attribute is read and nothing is reported; a material reading an attribute the packed points
really do not have still reports it. Generated scene only, CPU, small resolution."""

import json

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties
from cycles_houdini.device_selection import set_devices
from support.paths import artifacts_root

out = artifacts_root() / "stage2/packed-collapsed-warning"
out.mkdir(parents=True, exist_ok=True)
W, H = 240, 60
mat = ch.create_material(name="collapsed_emission", shader="emission")
shader = mat.node("surface_shader")
shader.setParms(dict(strength=4.0))
attribute = mat.createNode("cycles::attribute::1", "instance_attribute")
attribute.parm("attribute").set("Alpha")
shader.setNamedInput("strength", attribute, "fac")
obj = hou.node("/obj").createNode("geo", "born", run_init_scripts=False)
object_properties.install(obj)
sphere = obj.createNode("sphere")
sphere.setParms({"type": 2})
sphere.parmTuple("rad").set((0.2, 0.2, 0.2))
points = obj.createNode("attribwrangle", "points")
points.parm("class").set(0)
# Frame 1: three particles, all newborn (scale 0). Frame 2: the three have grown, two more are born.
points.parm("snippet").set(
    "int n = @Frame < 2 ? 3 : 5; for (int i = 0; i < n; i++) { int p = addpoint(0, set(-4 + 2 * i, 0, 0)); "
    'setpointattrib(0, "pscale", p, (@Frame >= 2 && i < 3) ? 1.0 : 0.0); }'
)
copy = obj.createNode("copytopoints::2.0", "copy")
copy.setInput(0, sphere)
copy.setInput(1, points)
copy.parm("pack").set(1)
fields = obj.createNode("attribwrangle", "fields")
fields.setInput(0, copy)
fields.parm("class").set(2)
fields.parm("snippet").set("f@Alpha = 1.0;")
fields.setDisplayFlag(True)
fields.setRenderFlag(True)
obj.parm("shop_materialpath").set(mat.path())
cam = hou.node("/obj").createNode("cam", "camera")
cam.setParms(dict(tz=10, resx=W, resy=H))
cam.parm("projection").set("ortho")
cam.parm("orthowidth").set(12)
rop = hou.node("/out").createNode("cycles_ipr", "collapsed_rop")
set_devices(rop, "CPU")
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        camera=cam.path(),
        resx=W,
        resy=H,
        samples=4,
        ipr_samples=4,
        use_denoise=0,
        ipr_use_denoise=0,
        world_source="color",
        allowmotionblur=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
rows, failures = [], []


def render(session, label, frame):
    hou.setFrame(frame)
    image = session.wait(session.apply(native_scene.capture(rop)))
    status = dict(session.status())
    plane = image["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(H, W, plane["channels"])
    columns = (pixels[:, :, :3].sum(2) > 0.05).any(0)
    row = dict(
        label=label,
        frame=frame,
        packed_prims=len(fields.geometry().prims()),
        visible=int((columns[1:] & ~columns[:-1]).sum() + int(columns[0])),
        missing=[
            i.get("attribute")
            for i in status.get("source_diagnostics", [])
            if i.get("code") == "material.missing_attribute"
        ],
    )
    rows.append(row)
    return row


def check(name, ok, row):
    if not ok:
        failures.append((name, row))


with ch.Session(
    device="CPU", width=W, height=H, samples=4, collect_diagnostics=True, use_texture_cache=False
) as session:
    row = render(session, "all instances collapsed", 1)
    check(
        "all collapsed: nothing visible, no missing-attribute warning", row["visible"] == 0 and not row["missing"], row
    )
    row = render(session, "three visible, two collapsed", 2)
    check("visible instances read the attribute, no warning", row["visible"] == 3 and not row["missing"], row)
    row = render(session, "all collapsed again", 1)
    check("all collapsed again: no warning", row["visible"] == 0 and not row["missing"], row)
    attribute.parm("attribute").set("not_there")
    row = render(session, "visible instances, attribute really missing", 2)
    check("a really missing attribute is still reported", row["missing"] == ["not_there"], row)
with ch.Session(
    device="CPU", width=W, height=H, samples=4, collect_diagnostics=True, use_texture_cache=False
) as session:
    attribute.parm("attribute").set("Alpha")
    row = render(session, "new session starting on the collapsed frame", 1)
    check("new session, all collapsed: no warning", row["visible"] == 0 and not row["missing"], row)

report = dict(result="failed" if failures else "passed", rows=rows, failures=failures)
(out / "collapsed-instance-warning.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
print("COLLAPSED " + json.dumps(report))
assert not failures, failures
