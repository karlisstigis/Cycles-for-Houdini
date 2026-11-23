# SPDX-License-Identifier: Apache-2.0
"""Override-aware reachability, texture ownership and independent displacement."""

from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import registry
from light_shader_fixture import Fixture, write_image

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
configure_render(f.rop, dict(use_material_assignments=1, samples=8, ipr_samples=8, pass_position=1))
builder = ch.create_material(name="override_graph_source", shader="emission")
shader = builder.node("surface_shader")
shader.parmTuple("color").set((1, 1, 1))
shader.parm("strength").set(1)
math = builder.createNode("cycles::math::1", "operation")
math.setParms(dict(math_type="add", value1=2.2, value2=3))
ch.promote_parameter(builder, math, "math_type", name="operation")
ch.promote_parameter(builder, math, "value2", name="operand")
shader.setNamedInput("strength", math, "value")
sop = f.obj.createNode("material", "native_graph_overrides")
sop.setInput(0, f.sop)
sop.setParms(
    dict(
        shop_materialpath1=builder.path(),
        num_local1=1,
        local1_name1="operation",
        local1_type1="string",
        local1_sval1="multiply",
    )
)
sop.setDisplayFlag(True)
sop.setRenderFlag(True)
entry = f.session()
dest = f.dest.parent / "material-override-graphs"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])


def render(
    label, expected, code=None, z=None, *, retain_geometry=False, retain_displacement=False, retain_subdivision=False
):
    previous = entry.session.status()
    frame = f.update()
    status = entry.session.status()
    actual = f.pixels(frame)[31:33, 31:33, :3].mean((0, 1))
    np.testing.assert_allclose(actual, np.broadcast_to(expected, (3,)), atol=1e-5, rtol=1e-5)
    codes = {d["code"] for d in status["source_diagnostics"]}
    assert bool(codes) if code == "any" else codes == ({code} if code else set()), status
    assert status["state"] == "complete" and status["render_device_ids"] == [f.device], status
    if z is not None:
        np.testing.assert_allclose(f.pixels(frame, "position")[31:33, 31:33, 2], z, atol=1e-5, rtol=0)
    if retain_geometry:
        assert status["geometry_conversions"] == previous["geometry_conversions"], (previous, status)
    if retain_displacement:
        assert status["displacement_rebuilds"] == previous["displacement_rebuilds"], (previous, status)
    if retain_subdivision:
        assert status["subdivision_tessellations"] == previous["subdivision_tessellations"], (previous, status)
    if retain_geometry and retain_displacement:
        assert not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys()
    report["checks"].append(dict(label=label, rgb=actual.tolist(), status=status))
    return frame


try:
    render("Promoted operation overrides use the matching engine inputs", 6.6)
    sop.parm("local1_sval1").set("round")
    render("Unary operation ignores inactive operands", 2, retain_geometry=True, retain_displacement=True)
    sop.parm("local1_sval1").set("unknown")
    render(
        "Invalid operation retains the authored operation",
        5.2,
        "material.override_ignored",
        retain_geometry=True,
        retain_displacement=True,
    )
    sop.setParms(dict(num_local1=2, local1_sval1="round", local1_name2="operand", local1_type2="float", local1_fval2=9))
    math.parm("value2").setExpression('ch("../operand")*2', hou.exprLanguage.Hscript)
    render(
        "Inactive arithmetic references do not produce override warnings",
        2,
        retain_geometry=True,
        retain_displacement=True,
    )
    sop.parm("local1_sval1").set("multiply")
    render("Active arithmetic evaluates the overridden operand", 39.6, retain_geometry=True, retain_displacement=True)
    math.parm("value2").setExpression('hou.parm("../operand").eval()*2', hou.exprLanguage.Python)
    render(
        "Active Python expression omits only its operand override",
        13.2,
        "material.override_ignored",
        retain_geometry=True,
        retain_displacement=True,
    )
    math.parm("value2").set(builder.parm("operand"), follow_parm_reference=False)
    render(
        "Reference repair retains the independent operation override",
        19.8,
        retain_geometry=True,
        retain_displacement=True,
    )
    shader.setInput(shader.inputNames().index("strength"), None)
    image = builder.createNode("cycles::image_texture::1", "image")
    image.parm("colorspace").set("data")
    image.parm("interpolation").set("closest")
    coordinates = builder.createNode("cycles::texture_coordinate::1", "coordinates")
    image.setNamedInput("vector", coordinates, "generated")
    shader.setNamedInput("color", image, "color")
    base = dest / "base.exr"
    replacement = dest / "replacement.exr"
    missing = dest / "repaired.exr"
    if missing.exists():
        missing.unlink()
    write_image(base, (0.1, 0.2, 0.4))
    write_image(replacement, (0.5, 0.25, 0.1))
    image.parm("filename").set(str(base))
    ch.promote_parameter(builder, image, "filename", name="image_file")
    displacement = builder.createNode("cycles::vector_displacement::1", "offset")
    # The default this test was written for (new nodes now take Blender's).
    displacement.setParms(dict(scale=1.0, space="object"))
    displacement.parmTuple("vector").set((0, 0, 0.1))
    offset = ch.promote_parameter(builder, displacement, "vector", name="offset")
    builder.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    builder.parm("cycles_displacement_method").set("true")
    sop.setParms(
        dict(
            local1_name1="image_file",
            local1_sval1=str(replacement),
            local1_name2=offset[2].name(),
            local1_type2="float",
            local1_fval2=0.25,
        )
    )
    render("String texture and geometric displacement overrides both work", (0.5, 0.25, 0.1), z=0.25)
    sop.parm("local1_sval1").set(str(missing))
    render(
        "Broken overridden color texture preserves independent displacement",
        (1, 0, 1),
        "any",
        0.25,
        retain_geometry=True,
        retain_displacement=True,
    )
    rebuilt = entry.session.status()["displacement_rebuilds"]
    write_image(missing, (0.25, 0.5, 0.75))
    render("Repairing the override file restores color and clears diagnostics", (0.25, 0.5, 0.75), z=0.25)
    assert entry.session.status()["displacement_rebuilds"] == rebuilt, (
        "Color-file repair repeated independent displacement"
    )
    report["repair_diagnostics"] = entry.session.diagnostics()
    assert not {"tri_verts", "tri_vindex"} & report["repair_diagnostics"]["buffer_copy_requests"].keys()
    sop.parm("local1_name2").set("unrecognized")
    render(
        "Invalid displacement override retains the authored displacement",
        (0.25, 0.5, 0.75),
        "material.override_ignored",
        0.1,
        retain_geometry=True,
    )
    sop.parm("local1_name2").set(offset[2].name())
    render("Repairing the displacement parameter restores its value", (0.25, 0.5, 0.75), z=0.25, retain_geometry=True)
    configure_render(f.rop, dict(subdivision=dict(type="catmull_clark", space="pixel", dicing_rate=4, max_level=4)))
    render("Adaptive subdivision preserves the overridden displacement", (0.25, 0.5, 0.75), z=0.25)
    assert entry.session.status()["subdivision_tessellations"] > 0
    sop.parm("local1_sval1").set(str(replacement))
    render(
        "Color filename edit retains adaptive geometry",
        (0.5, 0.25, 0.1),
        z=0.25,
        retain_geometry=True,
        retain_displacement=True,
        retain_subdivision=True,
    )
    missing.unlink()
    sop.parm("local1_sval1").set(str(missing))
    render(
        "Broken color texture retains adaptive geometry",
        (1, 0, 1),
        "any",
        0.25,
        retain_geometry=True,
        retain_displacement=True,
        retain_subdivision=True,
    )
    write_image(missing, (0.25, 0.5, 0.75))
    render(
        "Color file repair retains adaptive geometry",
        (0.25, 0.5, 0.75),
        z=0.25,
        retain_geometry=True,
        retain_displacement=True,
        retain_subdivision=True,
    )
    ch.promote_parameter(builder, shader, "strength", name="power")
    sop.setParms(dict(num_local1=3, local1_name3="power", local1_type3="float", local1_fval3=1))
    warm = []
    before = entry.session.status()
    for index in range(64):
        power = 1 + (index + 1) / 1000
        sop.parm("local1_fval3").set(power)
        frame = f.update()
        status = entry.session.status()
        np.testing.assert_allclose(
            f.pixels(frame)[31:33, 31:33, :3],
            np.broadcast_to(np.array((0.25, 0.5, 0.75)) * power, (2, 2, 3)),
            atol=2e-5,
            rtol=0,
        )
        for field in (
            "geometry_conversions",
            "displacement_rebuilds",
            "subdivision_tessellations",
            "engine_shader_resources",
        ):
            assert status[field] == before[field], (field, before, status)
        assert not {"tri_verts", "tri_vindex"} & entry.session.diagnostics()["buffer_copy_requests"].keys()
        assert not status["source_diagnostics"], status
        warm.append(
            {
                name: status[name]
                for name in (
                    "owned_geometry_bytes",
                    "device_allocation_bytes",
                    "engine_shader_resources",
                    "retired_shader_resources",
                )
            }
        )
    assert len({item["owned_geometry_bytes"] for item in warm}) == 1, warm
    report["warm_unique_overrides"] = warm
    color = np.array((0.25, 0.5, 0.75)) * power
    f.sop.parm("tz").set(0.2)
    render("Actual source position change updates adaptive displaced geometry", color, z=0.45)
    assert entry.session.status()["geometry_conversions"] > before["geometry_conversions"]
    assert entry.session.status()["displacement_rebuilds"] > before["displacement_rebuilds"]
    f.sop.parm("tz").set(0)
    render("Source position repair restores the original surface", color, z=0.25)
    report.update(result="passed")
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
