# SPDX-License-Identifier: Apache-2.0
"""Animated native OBJ material: shutter displacement must retain nominal surface fields."""

from support.paths import artifacts_root

from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from shader_curve_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/displacement-surface-fields" / args.device.replace(":", "_")
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(args.device, width=48, height=32)
f.grid.parm("ty").setExpression(".05*sin($T*8)", hou.exprLanguage.Hscript)
fields = f.obj.createNode("attribwrangle", "SURFACE_FIELDS")
fields.setInput(0, f.grid)
fields.parm("snippet").set("i@phase_0=3; f@phase_2=.4; v@v=set(0,.4*cos(@Time*8),0);")
fields.setDisplayFlag(True)
fields.setRenderFlag(True)
f.obj.parm("shop_materialpath").set(f.builder.path())
phase = f.builder.createNode("cycles::attribute::1")
phase.parm("attribute").set("phase_0")
phase2 = f.builder.createNode("cycles::attribute::1")
phase2.parm("attribute").set("phase_2")
scale = f.builder.createNode("cycles::math::1")
scale.setParms(dict(math_type="multiply", value2=0.1))
scale.setNamedInput("value1", phase, "fac")
add = f.builder.createNode("cycles::math::1")
add.parm("math_type").set("add")
add.setNamedInput("value1", scale, "value")
add.setNamedInput("value2", phase2, "fac")
f.emission.setNamedInput("color", add, "value")
displace = f.builder.createNode("cycles::displacement::1")
displace.setParms(dict(space="object", height=0.75, midlevel=0.5, scale=0.01))
output = f.builder.node("surface_output")
configure_render(
    f.rop,
    dict(
        source_mode="objects",
        vobject=f.obj.path(),
        materialpath="",
        use_material_assignments=1,
        samples=1,
        ipr_samples=1,
        use_denoise=0,
        ipr_use_denoise=0,
    ),
)
checks = []
report = dict(result="running", device=args.device, checks=checks)
with ch.Session(
    device=args.device,
    width=48,
    height=32,
    samples=1,
    execution="worker",
    interactive=True,
    threads=4,
    passes=["combined"],
) as s:
    pid = None

    def render(label, expected=0.7, broken=False):
        global pid
        frame = s.wait(s.apply(native_scene.capture(f.rop)), timeout=180)
        status = s.status()
        assert not status["error"], status
        if pid is None:
            pid = status["renderer_pid"]
        assert status["renderer_pid"] == pid, "IPR worker restarted during edits"
        issues = status["source_diagnostics"]
        if broken:
            assert any(
                v["code"] == "material.missing_attribute" and v["attribute"] == "phase_missing" for v in issues
            ), issues
        else:
            assert not issues, issues
        plane = frame["planes"]["combined"]
        image = np.frombuffer(plane["pixels"], np.float32).reshape(32, 48, plane["channels"])
        np.testing.assert_allclose(image[16, 24, :3], (1, 0, 1) if broken else (expected,) * 3, atol=3e-5, rtol=3e-5)
        checks.append(
            dict(
                label=label,
                material_motion_cooks=status["material_motion_cooks"],
                displacement_rebuilds=status["displacement_rebuilds"],
            )
        )

    for motion in ("velocity", "deformation"):
        for subdiv in ("none", "catmull_clark"):
            configure_render(
                f.rop, dict(allowmotionblur=1, geometry_motion=motion, subdivision=dict(type=subdiv, max_level=2))
            )
            for connected in (False, True, False, True):
                output.setInput(output.inputIndex("displacement"), displace if connected else None)
                f.builder.parm("cycles_displacement_method").set("true" if connected else "bump")
                render(f"{motion}/{subdiv}/displacement={connected}")
    phase.parm("attribute").set("phase_missing")
    render("Actual missing field diagnosed", broken=True)
    phase.parm("attribute").set("phase_0")
    render("Field repaired during IPR")
    fields.parm("snippet").set("i@phase_0=5; f@phase_2=.4; v@v=set(0,.4*cos(@Time*8),0);")
    render("Field edited during IPR", expected=0.9)
    s.cancel()
    render("Stop and restart", expected=0.9)
    s.close(wait=True)
    status = s.status()
    assert status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, status
report["result"] = "passed"
(dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks), device=args.device)))
