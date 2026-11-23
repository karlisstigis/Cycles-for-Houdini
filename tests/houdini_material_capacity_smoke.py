# SPDX-License-Identifier: Apache-2.0
"""Large native override populations recover by layer and retain base geometry."""

import pass_fixtures as pass_fixture

import argparse, json, time, traceback
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import registry, final_render, device_selection
from light_shader_fixture import Fixture, write_image
from curve_capacity_fixture import verify_curve_capacity

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
devices = json.loads(args.device) if args.device.startswith("[") else [args.device]
f = Fixture(devices[0])
device_selection.set_devices(f.rop, devices)
pass_fixture.set_parameters(f.rop, dict(use_material_assignments=1, samples=8, ipr_samples=8))
material = ch.create_material(name="capacity_source", shader="emission")
surface = material.node("surface_shader")
surface.parmTuple("color").set((0.1, 0.2, 0.3))
pass_fixture.parm(surface, "strength").set(1)
ch.promote_parameter(material, surface, "color", name="tint")
ch.promote_parameter(material, surface, "strength", name="power")
pass_fixture.parm(f.obj, "shop_materialpath").set(material.path())
pass_fixture.set_parameters(f.sop, dict(rows=2, cols=5011))
detail = f.obj.createNode("attribwrangle", "lower_detail_override")
detail.setInput(0, f.sop)
pass_fixture.set_parameters(detail, dict(**{"class": 0}, snippet="s@material_override=\"{'tint':(0.2,0.4,0.6)}\";"))
values = f.obj.createNode("attribwrangle", "many_primitive_overrides")
values.setInput(0, detail)
values.addSpareParmTuple(hou.IntParmTemplate("distinct", "Distinct Overrides", 1, default_value=(5000,)))
pass_fixture.set_parameters(
    values,
    dict(
        **{"class": 1},
        snippet='s@material_override=sprintf("{\'power\':%.8f}",2.0+float(@primnum%chi("distinct"))*0.000001);',
    ),
)
values.setDisplayFlag(True)
values.setRenderFlag(True)
entry = f.session()
dest = f.dest.parent / "material-capacity"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=args.device, checks=[])


def render(label, expected, warning=False, geometry=1):
    frame = f.update()
    status = entry.session.status()
    actual = f.pixels(frame)[31:33, 31:33, :3].mean((0, 1))
    np.testing.assert_allclose(actual, expected, atol=2e-5, rtol=0)
    issues = status["source_diagnostics"]
    assert {d["code"] for d in issues} == ({"material.override_capacity"} if warning else set()), status
    assert (
        status["state"] == "complete"
        and status["geometry_resources"] == geometry
        and status["render_device_ids"] == devices
    ), status
    report["checks"].append(dict(label=label, rgb=actual.tolist(), status=status))
    return frame


def output(node):
    node.setDisplayFlag(True)
    node.setRenderFlag(True)


def particle_render(label, power, warning):
    frame = f.update()
    status = entry.session.status()
    actual = f.pixels(frame)[31:33, 31:33, :3].mean((0, 1))
    np.testing.assert_allclose(actual[1:], np.array((0.4, 0.6)) * power, atol=2e-5, rtol=0)
    assert 0.199 * power <= actual[0] <= 0.264 * power, actual
    assert {d["code"] for d in status["source_diagnostics"]} == (
        {"material.override_capacity"} if warning else set()
    ), status
    assert status["state"] == "complete" and status["geometry_resources"] == 1 and status["object_resources"] == 1, (
        status
    )
    assert len(entry.session.native_material_references()) == 64
    report["checks"].append(dict(label=label, rgb=actual.tolist(), status=status))


try:
    render("Five thousand primitive overrides retain the lower detail layer", (0.2, 0.4, 0.6), True)
    assert len(entry.session.native_material_references()) == 1
    assert any("primitive override layer" in d["action"] for d in entry.session.status()["source_diagnostics"])
    pass_fixture.parm(values, "distinct").set(1)
    render("Repairing cardinality restores the primitive override", (0.4, 0.8, 1.2))
    pass_fixture.parm(values, "distinct").set(5000)
    render("Excessive overrides remain recoverable on repeated edits", (0.2, 0.4, 0.6), True)
    conversions = entry.session.status()["geometry_conversions"]
    entry.session.wait(entry.apply(dict(camera_path=f.camera.path()), hou.time()))
    assert entry.session.status()["geometry_conversions"] == conversions
    assert {d["code"] for d in entry.session.status()["source_diagnostics"]} == {"material.override_capacity"}
    pass_fixture.parm(values, "distinct").set(1)
    render("A second repair clears the retained capacity warning", (0.4, 0.8, 1.2))
    pass_fixture.parm(values, "distinct").set(4096)
    frame = f.update()
    status = entry.session.status()
    actual = f.pixels(frame)[31:33, 31:33, :3].mean((0, 1))
    ratios = actual / np.array((0.2, 0.4, 0.6))
    assert np.all((ratios >= 1.99999) & (ratios <= 2.00411)), actual
    assert not status["source_diagnostics"] and len(entry.session.native_material_references()) == 4096, status
    report["checks"].append(
        dict(label="Exactly 4096 override combinations remain supported", rgb=actual.tolist(), status=status)
    )
    pass_fixture.parm(values, "distinct").set(1)
    render("Reducing a supported population retains its values", (0.4, 0.8, 1.2))
    # A packed container must omit its excessive layer before expansion, so all
    # five thousand compatible copies keep one shared prototype.
    box = f.obj.createNode("box", "capacity_prototype")
    box.parmTuple("size").set((0.05, 0.3, 0.2))
    targets = f.obj.createNode("line", "five_thousand_targets")
    pass_fixture.set_parameters(targets, dict(points=5000, dist=4))
    targets.parmTuple("dir").set((1, 0, 0))
    targets.parmTuple("origin").set((-2, 0, 0))
    copies = f.obj.createNode("copytopoints", "packed_capacity")
    copies.setInput(0, box)
    copies.setInput(1, targets)
    pass_fixture.parm(copies, "pack").set(1)
    detail.setInput(0, copies)
    pass_fixture.parm(values, "distinct").set(5000)
    render("Excessive packed overrides retain a single shared prototype", (0.2, 0.4, 0.6), True)
    assert entry.session.status()["packed_instances"] == 5000
    pass_fixture.parm(values, "distinct").set(1)
    render("Packed override repair retains sharing", (0.4, 0.8, 1.2))
    assert entry.session.status()["packed_instances"] == 5000
    # Neither owner exceeds the limit alone: 64 primitive layers combined with
    # 128 point layers make 8192 actual particle bindings. Drop only point values.
    line = f.obj.createNode("line", "particle_row")
    pass_fixture.set_parameters(line, dict(points=128, dist=3.8))
    line.parmTuple("dir").set((1, 0, 0))
    line.parmTuple("origin").set((-1.9, 0, 0))
    particle = f.obj.createNode("add", "one_particle_primitive")
    particle.setInput(0, line)
    pass_fixture.set_parameters(particle, dict(keep=1, addparticlesystem=1))
    rows = f.obj.createNode("line", "particle_rows")
    pass_fixture.set_parameters(rows, dict(points=64, dist=3.8))
    rows.parmTuple("origin").set((0, -1.9, 0))
    systems = f.obj.createNode("copytopoints", "particle_combinations")
    systems.setInput(0, particle)
    systems.setInput(1, rows)
    pass_fixture.parm(systems, "pack").set(0)
    assert len(systems.geometry().prims()) == 64
    detail.setInput(0, systems)
    primitive = f.obj.createNode("attribwrangle", "sixty_four_primitive_layers")
    primitive.setInput(0, detail)
    pass_fixture.set_parameters(
        primitive,
        dict(**{"class": 1}, snippet="s@material_override=sprintf(\"{'tintr':%.4f}\",0.2+float(@primnum%64)*0.001);"),
    )
    points = f.obj.createNode("attribwrangle", "one_twenty_eight_point_layers")
    points.setInput(0, primitive)
    points.addSpareParmTuple(hou.IntParmTemplate("distinct", "Point Variants", 1, default_value=(128,)))
    pass_fixture.set_parameters(
        points,
        dict(
            **{"class": 2},
            snippet='f@pscale=0.06;s@material_override=sprintf("{\'power\':%.4f}",2.0+float(@ptnum%chi("distinct"))*0.001);',
        ),
    )
    output(points)
    particle_render(
        "Combined particle layers retain primitive overrides when point combinations exceed the budget", 1, True
    )
    assert any("point override layer" in d["action"] for d in entry.session.status()["source_diagnostics"])
    pass_fixture.parm(points, "distinct").set(1)
    particle_render("Repair restores supported point and primitive combinations", 2, False)
    pass_fixture.parm(points, "distinct").set(128)
    particle_render("Repeated combined-layer recovery remains local", 1, True)
    pass_fixture.parm(points, "distinct").set(1)
    particle_render("Combined-layer repair clears diagnostics", 2, False)
    switched = ch.create_material(name="point_switch_target", shader="emission")
    switched.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.6))
    pass_fixture.parm(switched.node("surface_shader"), "strength").set(1)
    ch.promote_parameter(switched, switched.node("surface_shader"), "strength", name="power")
    point_snippet = pass_fixture.evaluate(points, "snippet")
    pass_fixture.parm(points, "distinct").set(128)
    pass_fixture.parm(points, "snippet").set(point_snippet + 's@shop_materialpath="' + switched.path() + '";')
    frame = f.update()
    status = entry.session.status()
    refs = entry.session.native_material_references()
    assert not status["source_diagnostics"] and len(refs) == 128, status
    assert {r["path"] for r in refs} == {switched.path()}, refs[:3]
    ratios = f.pixels(frame)[31:33, 31:33, 1] / 0.4
    assert np.all((ratios >= 1.99999) & (ratios <= 2.12701)), ratios
    report["checks"].append(
        dict(
            label="Point material switches count only effective override combinations",
            references=len(refs),
            status=status,
        )
    )
    pass_fixture.parm(points, "snippet").set(point_snippet)
    pass_fixture.parm(points, "distinct").set(1)
    particle_render("Removing the point switch restores primitive inheritance", 2, False)
    mixed_detail = f.obj.createNode("attribwrangle", "mixed_mesh_detail")
    mixed_detail.setInput(0, f.sop)
    pass_fixture.set_parameters(
        mixed_detail, dict(**{"class": 0}, snippet="s@material_override=\"{'tint':(0.2,0.4,0.6)}\";")
    )
    mixed_mesh = f.obj.createNode("attribwrangle", "mixed_mesh_capacity")
    mixed_mesh.setInput(0, mixed_detail)
    pass_fixture.parm(mixed_mesh, "class").set(1)
    pass_fixture.parm(mixed_mesh, "snippet").set(
        "s@material_override=sprintf(\"{'power':%.8f}\",3.0+float(@primnum)*0.000001);"
    )
    combined = f.obj.createNode("merge", "mixed_capacity_components")
    combined.setInput(0, mixed_mesh)
    combined.setInput(1, points)
    output(combined)
    pass_fixture.parm(f.camera, "orthowidth").set(6)

    def mixed_render(label, mesh_power, point_power, warning):
        frame = f.update()
        status = entry.session.status()
        pixels = f.pixels(frame)
        np.testing.assert_allclose(pixels[4, 4, :3], np.array((0.2, 0.4, 0.6)) * mesh_power, atol=2e-5, rtol=0)
        np.testing.assert_allclose(
            pixels[31:33, 31:33, 1:],
            np.broadcast_to((0.4 * point_power, 0.6 * point_power, 1), (2, 2, 3)),
            atol=2e-5,
            rtol=0,
        )
        issues = status["source_diagnostics"]
        assert status["state"] == "complete" and status["geometry_resources"] == 2, status
        assert {d["code"] for d in issues} == ({"material.override_capacity"} if warning else set()), issues
        assert len(issues) == int(warning) and all(d["source"] == combined.path() for d in issues), issues
        report["checks"].append(dict(label=label, status=status))

    mixed_render("Mixed mesh capacity fallback leaves supported point overrides intact", 1, 2, True)
    pass_fixture.parm(mixed_mesh, "snippet").set("s@material_override=\"{'power':3}\";")
    mixed_render("Repairing one mixed component clears only its warning", 3, 2, False)
    pass_fixture.parm(points, "distinct").set(128)
    mixed_render("A new point capacity failure leaves the repaired mesh material intact", 3, 1, True)
    pass_fixture.parm(points, "distinct").set(1)
    mixed_render("Repairing the point component restores both mixed bindings", 3, 2, False)
    pass_fixture.parm(f.camera, "orthowidth").set(4)
    # The capacity policy constrains overrides, not ordinary assignment paths or
    # their identities. Missing paths remain separate magenta assignments.
    detail.setInput(0, f.sop)
    paths = f.obj.createNode("attribwrangle", "many_assignment_paths")
    paths.setInput(0, detail)
    paths.addSpareParmTuple(hou.ToggleParmTemplate("broken", "Missing Paths", default_value=True))
    pass_fixture.set_parameters(
        paths,
        dict(
            **{"class": 1},
            snippet='s@shop_materialpath=chi("broken")?sprintf("/mat/capacity_missing_%d",@primnum):"'
            + material.path()
            + '";s@material_override="";',
        ),
    )
    pass_fixture.set_parameters(f.rop, dict(crypto_material=1, crypto_levels=2))
    output(paths)
    frame = f.update()
    status = entry.session.status()
    np.testing.assert_allclose(
        f.pixels(frame)[31:33, 31:33, :3], np.broadcast_to((1, 0, 1), (2, 2, 3)), atol=2e-5, rtol=0
    )
    assert {d["code"] for d in status["source_diagnostics"]} == {"material.missing"} and status[
        "state"
    ] == "complete", status
    references = entry.session.native_material_references()
    assert len(references) == 5010 and len({r["path"] for r in references}) == 5010
    metadata = entry.session.output_metadata()
    prefix = next(key[:-4] for key, value in metadata.items() if key.endswith("/name") and value == "CryptoMaterial")
    names = json.loads(metadata[prefix + "manifest"])
    assert all("/mat/capacity_missing_" + str(index) in names for index in range(5010))
    report["checks"].append(
        dict(
            label="More than 4096 ordinary paths keep independent fallback and Cryptomatte identities",
            references=len(references),
            crypto_names=len(names),
            diagnostics=len(status["source_diagnostics"]),
        )
    )
    pass_fixture.parm(paths, "broken").set(False)
    render("Repairing the large path population restores the assigned material", (0.2, 0.4, 0.6))
    pass_fixture.parm(f.rop, "crypto_material").set(0)
    # Each literal is valid individually, but their combined owned payload is
    # larger than the component budget. Omit the layer before opening its files.
    byte_material = ch.create_material(name="byte_budget_source", shader="emission")
    byte_surface = byte_material.node("surface_shader")
    pass_fixture.parm(byte_surface, "strength").set(1)
    image = dest / "byte-budget.exr"
    write_image(image, (0.2, 0.4, 0.6))
    texture = byte_material.createNode("cycles::image_texture::1", "byte_color")
    pass_fixture.set_parameters(texture, dict(filename=str(image), colorspace="data"))
    coords = byte_material.createNode("cycles::texture_coordinate::1", "byte_coordinates")
    texture.setNamedInput("vector", coords, "generated")
    byte_surface.setNamedInput("color", texture, "color")
    ch.promote_parameter(byte_material, byte_surface, "strength", name="power")
    ch.promote_parameter(byte_material, texture, "filename", name="image_file")
    pass_fixture.parm(f.obj, "shop_materialpath").set(byte_material.path())
    pass_fixture.parm(f.sop, "cols").set(401)
    pass_fixture.parm(detail, "snippet").set("s@material_override=\"{'power':3}\";")
    original_snippet = pass_fixture.evaluate(values, "snippet")
    pass_fixture.parm(values, "snippet").set(
        "s@material_override=sprintf(\"{'image_file':'" + "x" * 30000 + "_%d','power':4}\",@primnum);"
    )
    output(values)
    render("Excessive owned literals retain the valid base image and detail layer", (0.6, 1.2, 1.8), True)
    assert all(
        "8 MiB" in d["message"] and d["source"] == values.path() for d in entry.session.status()["source_diagnostics"]
    )
    pass_fixture.parm(values, "snippet").set("s@material_override=\"{'power':4}\";")
    render("Repairing the literal payload clears its warning", (0.8, 1.6, 2.4))
    verify_curve_capacity(f, entry, report, material, byte_material, dest, devices)
    pass_fixture.parm(f.obj, "shop_materialpath").set(material.path())
    pass_fixture.parm(f.sop, "cols").set(5011)
    pass_fixture.parm(detail, "snippet").set("s@material_override=\"{'tint':(0.2,0.4,0.6)}\";")
    pass_fixture.parm(values, "snippet").set(original_snippet)
    output(values)
    pass_fixture.parm(values, "distinct").setExpression("if($F == 3,5000,1)", hou.exprLanguage.Hscript)
    pass_fixture.parm(f.rop, "picture").set(str(dest / "frame.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    report["final"] = summary
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        channels = list(image.spec().channelnames)
        pixels = image.get_pixels()
        expected = np.array((0.2, 0.4, 0.6)) * (1 if index == 3 else 2)
        np.testing.assert_allclose(
            pixels[31:33, 31:33][:, :, [channels.index(c) for c in ("R", "G", "B")]].mean((0, 1)),
            expected,
            atol=2e-5,
            rtol=0,
        )
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3), issues
        batch.append(dict(frame=index, diagnostics=issues))
    report.update(batch=batch, final=summary)
    report.update(result="passed")
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    deadline = time.monotonic() + 15
    while entry.session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = entry.session.status()
    if report["result"] == "passed" and not (
        report["closed"]["state"] == "closed"
        and report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
    ):
        report.update(
            result="failed", error="Capacity IPR did not release its owned allocations: " + str(report["closed"])
        )
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
