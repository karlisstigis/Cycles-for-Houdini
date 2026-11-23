# SPDX-License-Identifier: Apache-2.0
import hou
import numpy as np
import cycles_houdini as ch
from volume_field_recovery_fixture import fixture as base_fixture


def fixture(device):
    rop, edit, base = base_fixture(device)
    templates = edit.parmTemplateGroup()
    control = templates.find("mode")
    control.setLabel("Volume Recovery Test")
    control.setHelp(
        "0: valid; 1: missing shader field; 2: conflicting grid materials; 3: both; 4: change density material; 5: density is the second grid; 6: required integer field."
    )
    templates.replace("mode", control)
    edit.setParmTemplateGroup(templates)
    dest = base.parent / "volume-shader-recovery"
    dest.mkdir(parents=True, exist_ok=True)
    material = hou.node("/mat/volume_absorption")
    field = material.createNode("cycles::attribute::1", "required_field")
    field.parm("attribute").set("extinction")
    material.node("volume_shader").setNamedInput("density", field, "fac")
    secondary = ch.create_material(name="volume_secondary", shader="absorption_volume")
    secondary.node("volume_shader").parmTuple("color").set((0, 0, 0))
    secondary.node("density_scale").parm("value2").set(0.5)
    edit.parm("python").set(
        """import hou
g=hou.pwd().geometry();mode=hou.pwd().evalParm('mode')
d=[p for p in g.prims() if p.attribValue('name')=='density'][:2]
if mode==6:
    extra=next(p for p in g.prims() if p.attribValue('name')=='integer_field')
    d=[d[0],extra]
g.addAttrib(hou.attribType.Prim,'shop_materialpath','')
for p in d:p.setAttribValue('shop_materialpath',%r)
density,extra=(d[1],d[0]) if mode==5 else (d[0],d[1])
extra.setAttribValue('name','unavailable' if mode in (1,3) else 'extinction')
if mode in (2,3,5):extra.setAttribValue('shop_materialpath',%r)
if mode==4:density.setAttribValue('shop_materialpath',%r)
g.deletePrims([p for p in g.prims() if p not in d],keep_points=False)
"""
        % (material.path(), secondary.path(), secondary.path())
    )
    neighbor = hou.node("/obj").createNode("geo", "valid_shared_material", run_init_scripts=False)
    stash = neighbor.createNode("stash", "valid_fields")
    stash.parm("stash").set(edit.geometry())
    stash.setDisplayFlag(True)
    stash.setRenderFlag(True)
    neighbor.parm("tx").set(-1.4)
    rop.parm("vobject").set(edit.parent().path() + " " + neighbor.path())
    rop.setParms(dict(samples=512, ipr_samples=512))
    return rop, edit, dest


def check(frame, status, mode):
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(48, 80, 4)
    assert np.isfinite(pixels).all() and status["state"] != "failed", status
    assert status["object_resources"] == 3, status
    assert pixels[24, 70, 1] > 0.6 and pixels[24, 70, 0] < 0.2, pixels[24, 70]
    # Independent absorption expectation for the unaffected shared material.
    neighbor = pixels[21:27, 10:14, :3].mean(axis=(0, 1))
    assert np.max(np.abs(neighbor - np.exp(-1.125))) < 0.065, neighbor.tolist()
    center = pixels[21:27, 37:43, :3].mean(axis=(0, 1))
    if mode in (1, 3, 6):
        assert center[0] > center[1] + 0.05 and center[2] > center[1] + 0.05, center.tolist()
    else:
        assert np.max(np.abs(center - np.exp(-1.125 * (0.5 if mode == 4 else 1)))) < 0.065, center.tolist()
    issues = status["source_diagnostics"]
    codes = [d["code"] for d in issues]
    assert codes.count("volume.shader_field_unavailable") == int(mode in (1, 3, 6)), issues
    assert codes.count("volume.conflicting_materials") == int(mode in (2, 3, 4, 5)), issues
    assert codes.count("volume.field_ignored") == int(mode == 6), issues
    if mode == 0:
        assert not issues, issues
    return pixels.copy()
