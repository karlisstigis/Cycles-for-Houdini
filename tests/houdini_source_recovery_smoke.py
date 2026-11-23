# SPDX-License-Identifier: Apache-2.0
"""Ordinary ROP source selection survives local failures and five batch frames."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
dest = artifacts_root() / "stage3/materials/recovery"
dest.mkdir(parents=True, exist_ok=True)
root = hou.node("/obj").createNode("subnet", "recovery_sources")
obj = root.createNode("geo", "mixed", run_init_scripts=False)
pig = obj.createNode("testgeometry_pighead")
unsupported = obj.createNode("sphere", "unsupported_primitive")
unsupported.parm("type").set("prim")
unsupported.parm("tx").set(2)
merged = obj.createNode("merge")
merged.setInput(0, pig)
merged.setInput(1, unsupported)
merged.setRenderFlag(True)
merged.setDisplayFlag(True)
camera = hou.node("/obj").createNode("cam", "sources_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
light = root.createNode("hlight", "optional_light_settings")
light.parmTuple("t").set((0, 3, 4))
light.parm("shadow_intensity").set(0.5)
rop = hou.node("/out").createNode("cycles_ipr", "recovery_rop")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=root.path(),
        camera=camera.path(),
        resx=96,
        resy=72,
        samples=4,
        ipr_samples=4,
        allowmotionblur=0,
        vobject="*",
        lights="*",
        use_material_assignments=1,
    )
)
checks = []

with ch.Session(device=device, width=96, height=72, samples=4, interactive=False) as session:
    frame = session.wait(session.apply(native_scene.capture(rop)), timeout=120)
    status = session.status()
    codes = {d["code"] for d in status["source_diagnostics"]}
    assert {"material.unsupported", "geometry.unsupported_primitive", "light.unsupported_setting"} <= codes, status
    assert status["light_resources"] == 1
    assert status["geometry_resources"] == 1, "Unsupported sphere vertices became extra point geometry"
    assert np.isfinite(np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)).all()
    checks.append("Mixed mesh/unsupported primitive keeps pig and supported light; no phantom particles")

    light.parm("shadow_intensity").set(1)
    merged.setInput(1, None)
    session.wait(session.apply(native_scene.capture(rop)))
    codes = {d["code"] for d in session.status()["source_diagnostics"]}
    assert codes == {"material.unsupported"}, codes
    checks.append("Removing unsupported information clears its diagnostics")

    broken_obj = root.createNode("geo", "failed_cook", run_init_scripts=False)
    broken_sop = broken_obj.createNode("python")
    broken_sop.parm("python").set('raise hou.NodeError("Deliberate source failure")')
    broken_sop.setRenderFlag(True)
    session.wait(session.apply(native_scene.capture(rop)))
    assert any(d["code"] == "geometry.cook_failed" for d in session.status()["source_diagnostics"])
    assert session.status()["geometry_resources"] == 1
    broken_sop.parm("python").set("")
    session.wait(session.apply(native_scene.capture(rop)))
    assert not any(d["code"] == "geometry.cook_failed" for d in session.status()["source_diagnostics"])
    broken_obj.destroy()
    checks.append("A failed SOP cook is isolated and repairing it clears the warning")

    rop.setParms(dict(world_source="material", world_material="/mat/missing_world"))
    session.wait(session.apply(native_scene.capture(rop)))
    assert any(d["code"] == "world.unavailable" for d in session.status()["source_diagnostics"])
    rop.parm("world_source").set("color")
    checks.append("Broken world assignment falls back without rejecting the render")

    rop.parm("materialpath").set("/mat/missing_override")
    session.wait(session.apply(native_scene.capture(rop)))
    assert any(d["code"] == "material.invalid_override" for d in session.status()["source_diagnostics"])
    rop.parm("materialpath").set("")
    checks.append("Broken ROP material override remains renderable")

# Batch has the same policy. Frame 3 is deliberately broken and frame 4 repairs it.
valid = ch.create_material(name="batch_recovery_surface", shader="emission")
valid.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
valid.node("surface_shader").parm("strength").set(1)
assignment = obj.createNode("attribwrangle")
assignment.setInput(0, pig)
assignment.parm("class").set(1)
# The Material SOP itself retains existing bindings for a nonexistent path.
# Author the actual broken attribute to exercise translation, not that SOP's guard.
assignment.parm("snippet").set(f's@shop_materialpath = @Frame == 3 ? "/mat/missing_batch_material" : "{valid.path()}";')
assignment.setRenderFlag(True)
assignment.setDisplayFlag(True)
rop.parm("picture").set(str(dest / "recovery-batch.$F4.exr"))
rop.render(frame_range=(1, 5, 1))
for number in range(1, 6):
    reader = OIIO.ImageInput.open(str(dest / f"recovery-batch.{number:04d}.exr"))
    assert reader
    spec = reader.spec()
    pixels = reader.read_image(format=OIIO.FLOAT)
    metadata = spec.get_string_attribute("cycles_houdini:source_diagnostics")
    reader.close()
    rgba = pixels[..., spec.channelnames.index("R") : spec.channelnames.index("R") + 3]
    if number == 3:
        assert metadata and "missing_batch_material" in metadata, (
            number,
            metadata,
            rop.cachedUserData("cycles_houdini.final_sequence"),
        )
        assert ((rgba[..., 0] > 0.8) & (rgba[..., 1] < 0.1) & (rgba[..., 2] > 0.8)).sum() > 100
    else:
        assert json.loads(metadata or "[]") == [], metadata
        assert ((rgba[..., 1] > 0.6) & (rgba[..., 0] < 0.2)).sum() > 100
checks.append("Five actual ROP frames complete; broken frame is marked and subsequent frames recover")
report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-source-recovery-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
