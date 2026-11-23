# SPDX-License-Identifier: Apache-2.0
"""Independent point/curve shader palettes, precedence and motion identities."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import argparse
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
requested_device = parser.parse_args().device

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/strands"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
colors = dict(red=(0.8, 0.1, 0.05), blue=(0.05, 0.1, 0.8), green=(0.05, 0.7, 0.1), fallback=(0.4, 0.1, 0.5))
builders = {}
for name in ("red", "blue", "green"):
    material = ch.create_material(name="strand_" + name, shader="emission")
    material.node("surface_shader").parmTuple("color").set(colors[name])
    material.node("surface_shader").parm("strength").set(1.0)
    builders[name] = material
obj = hou.node("/obj").createNode("geo", "strand_assignment_receiver", run_init_scripts=False)
source = obj.createNode("python", "sphere_points")
hair = obj.createNode("python", "linear_curves")
for node in (source, hair):
    for name, default in dict(bindings="[]", order="[0,1,2,3]", detailmat="", decorator="").items():
        node.addSpareParmTuple(hou.StringParmTemplate(name, name, 1, default_value=(default,)))
    for name in ("offset", "clock"):
        node.addSpareParmTuple(hou.FloatParmTemplate(name, name, 1, default_value=(0.0,)))
    node.parm("clock").setExpression("$T", hou.exprLanguage.Hscript)
    for name in ("reorder_motion", "animated_binding", "hide_first"):
        node.addSpareParmTuple(hou.ToggleParmTemplate(name, name, default_value=False))
common = """import hou,json
node=hou.pwd();g=node.geometry();bindings=json.loads(node.evalParm('bindings'));order=json.loads(node.evalParm('order'))
if node.evalParm('reorder_motion') and node.evalParm('clock')<0: order.reverse()
if node.evalParm('animated_binding') and node.evalParm('clock')<0: bindings[0]=bindings[1]
detail=node.evalParm('detailmat')
if detail: g.addAttrib(hou.attribType.Global,'shop_materialpath','');g.setGlobalAttribValue('shop_materialpath',detail)
g.addAttrib(hou.attribType.Point,'id',0);g.addAttrib(hou.attribType.Point,'Cd',(1.,1.,1.));g.addAttrib(hou.attribType.Point,'v',(2.4,0,0))
"""
source.parm("python").set(
    common
    + """
g.addAttrib(hou.attribType.Point,'pscale',.24);g.addAttrib(hou.attribType.Point,'shop_materialpath','');g.addAttrib(hou.attribType.Point,'dead',0)
for identity in order:
    p=g.createPoint();p.setPosition((identity*.8-1.2+node.evalParm('offset'),0,0));p.setAttribValue('id',identity)
    p.setAttribValue('shop_materialpath',bindings[identity] if bindings else '')
    p.setAttribValue('Cd',(.5,.25,1.) if identity==1 else (1.,1.,1.))
    p.setAttribValue('dead',int(identity==0 and node.evalParm('hide_first')))
exec(node.evalParm('decorator'))
"""
)
hair.parm("python").set(
    common
    + """
g.addAttrib(hou.attribType.Prim,'id',0);g.addAttrib(hou.attribType.Prim,'shop_materialpath','');g.addAttrib(hou.attribType.Point,'width',.12)
for identity in order:
    c=g.createPolygon(is_closed=False);c.setAttribValue('id',identity);c.setAttribValue('shop_materialpath',bindings[identity] if bindings else '')
    for k,y in enumerate((-.6,.6)):
        p=g.createPoint();p.setPosition((identity*.8-1.2+node.evalParm('offset'),y,0));p.setAttribValue('id',identity*2+k)
        p.setAttribValue('Cd',(.5,.25,1.) if identity==1 else (1.,1.,1.));c.addVertex(p)
exec(node.evalParm('decorator'))
"""
)
particles = obj.createNode("add", "first_particle_group")
particles.setInput(0, source)
particles.parm("addparticlesystem").set(1)
particles.parm("particlegroup").set("0 1")
overlap = obj.createNode("add", "overlapping_particle_group")
overlap.setInput(0, particles)
overlap.parm("addparticlesystem").set(1)
overlap.parm("particlegroup").set("1 2")
primitive_assignment = obj.createNode("material", "particle_primitive_materials")
primitive_assignment.setInput(0, overlap)
primitive_assignment.parm("num_materials").set(2)
for index, name in ((1, "red"), (2, "blue")):
    primitive_assignment.parm(f"group{index}").set(str(index - 1))
    primitive_assignment.parm(f"shop_materialpath{index}").set(builders[name].path())
point_assignment = obj.createNode("material", "point_material_sop")
point_assignment.setInput(0, source)
point_assignment.parm("style").set("point")
point_assignment.parm("group1").set("2")
point_assignment.parm("shop_materialpath1").set(builders["blue"].path())
packed_obj = hou.node("/obj").createNode("geo", "packed_strand_assignments", run_init_scripts=False)
prototype = packed_obj.createNode("python", "two_strand_prototype")
prototype.addSpareParmTuple(hou.ToggleParmTemplate("hair", "Hair", default_value=False))
prototype.addSpareParmTuple(hou.ToggleParmTemplate("fully_bound", "Fully Bound", default_value=False))
prototype.parm("python").set("""import hou
node=hou.pwd();g=node.geometry();hair=node.evalParm('hair');bound=node.evalParm('fully_bound')
g.addAttrib(hou.attribType.Point,'id',0)
if hair:
    g.addAttrib(hou.attribType.Prim,'shop_materialpath','');g.addAttrib(hou.attribType.Point,'width',.12)
else:
    g.addAttrib(hou.attribType.Point,'shop_materialpath','');g.addAttrib(hou.attribType.Point,'pscale',.12)
for i,x in enumerate((-.2,.2)):
    if hair:
        c=g.createPolygon(is_closed=False);c.setAttribValue('shop_materialpath','/mat/strand_blue' if i==0 or bound else '')
        for k,y in enumerate((-.3,.3)):
            p=g.createPoint();p.setPosition((x,y,0));p.setAttribValue('id',i*2+k);c.addVertex(p)
    else:
        p=g.createPoint();p.setPosition((x,0,0));p.setAttribValue('id',i);p.setAttribValue('shop_materialpath','/mat/strand_blue' if i==0 or bound else '')
""")
instances = packed_obj.createNode("python", "two_instances")
instances.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'id',0)\nfor i,x in enumerate((-.8,.8)):\n p=g.createPoint();p.setPosition((x,0,0));p.setAttribValue('id',10+i)"
)
copies = packed_obj.createNode("copytopoints", "packed_copies")
copies.setInput(0, prototype)
copies.setInput(1, instances)
copies.parm("pack").set(1)
packed = packed_obj.createNode("material", "packed_material_fallbacks")
packed.setInput(0, copies)
packed.parm("num_materials").set(2)
for i, name in ((1, "red"), (2, "green")):
    packed.parm(f"group{i}").set(str(i - 1))
    packed.parm(f"shop_materialpath{i}").set(builders[name].path())
packed_obj.parm("shop_materialpath").set("/mat/unused_missing_packed_strand_object")
camera = hou.node("/obj").createNode("cam", "strand_assignment_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr", "native_strand_assignment_cycles")
set_rop_devices(rop, "CPU")
for name, value in dict(
    source_mode="sop",
    soppath=source.path(),
    camera=camera.path(),
    resx=96,
    resy=96,
    samples=8,
    ipr_samples=8,
    adaptive_sampling=0,
    ipr_adaptive_sampling=0,
    sampling_pattern="tabulated_sobol",
    seed=0,
    animate_seed=0,
    pixel_filter="box",
    filter_width=1,
    film_exposure=1,
    material_kind="emission",
    use_denoise=0,
    ipr_use_denoise=0,
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor").set(colors["fallback"])
rop.parm("emission_strength").set(1.0)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
# The default this test was written for (before the object tab took Blender's defaults):
# Linear curves, as the engine reference renders them.
for strand_object in (obj, packed_obj):
    object_properties.install(strand_object)
    strand_object.parm("cycles_curve_shape").set("linear")


def reset():
    hou.setFrame(1)
    builders["green"].setName("strand_green")
    obj.parm("shop_materialpath").set(builders["green"].path())
    obj.parm("tx").set(0)
    camera.parm("tx").set(0)
    for node in (source, hair):
        node.parm("bindings").set(json.dumps([builders["red"].path(), builders["blue"].path(), "", ""]))
        node.parm("order").set("[0,1,2,3]")
        node.parm("detailmat").set("")
        node.parm("decorator").set("")
        node.parm("offset").deleteAllKeyframes()
        node.parm("offset").set(0)
        for name in ("reorder_motion", "animated_binding", "hide_first"):
            node.parm(name).set(0)
    for name, builder in builders.items():
        builder.node("surface_shader").parmTuple("color").set(colors[name])
        builder.node("surface_shader").parm("strength").set(1.0)
    rop.parm("soppath").set(source.path())
    rop.parm("allowmotionblur").set(0)
    rop.parm("geometry_motion").set("transform")
    rop.parm("use_material_assignments").set(1)
    rop.parm("materialpath").set("")


def capture(session):
    return session.wait(session.apply(native_scene.capture(rop)))


def sample(frame, index, expected):
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(
        frame["height"], frame["width"], 4
    )
    # The containing pixel remains inside the narrow curve over every
    # jitter/shutter sample. Neighbour columns can cross its silhouette.
    x = int(((index * 0.8 - 1.2) / 4 + 0.5) * frame["width"])
    y = frame["height"] // 2
    actual = pixels[y - 1 : y + 2, x, :3].mean(axis=0)
    assert np.allclose(actual, expected, atol=0.001, rtol=0), (index, actual, expected)
    return actual.tolist()


def expect(frame, names):
    return [sample(frame, index, colors[name] if name else (0, 0, 0)) for index, name in enumerate(names)]


def recovery(session):
    frame = capture(session)
    assert session.status()["source_diagnostics"] and not session.status()["error"]
    return frame


def reference_status(session):
    """Match the independent harness's explicit eight-sample core defaults."""
    status = session.status()
    assert status["rendering"]["samples"] == status["current_sample"] == 8, status
    expected = dict(
        adaptive_sampling=False,
        sampling_pattern="tabulated_sobol",
        seed=0,
        animate_seed=False,
        pixel_filter="box",
        filter_width=1,
        film_exposure=1,
    )
    assert {key: status["rendering"][key] for key in expected} == expected, status
    return status


assert ch.capabilities()["native_point_materials"] and ch.capabilities()["native_curve_materials"]
reports = []
for device in (requested_device,):
    reset()
    set_rop_devices(rop, device)
    checks = []
    with ch.Session(device=device, width=96, height=96, samples=8, collect_diagnostics=True) as session:
        frame = capture(session)
        expect(frame, ("red", "blue", "green", "green"))
        initial = reference_status(session)
        prefix = "cpu" if device == "CPU" else "optix"
        session.write_exr(destination / (prefix + "-points.exr"))
        assert initial["points"] == 4 and initial["geometry_resources"] == 1 and initial["material_resources"] == 4
        checks.append("Independent radiance for per-point, empty-handle and OBJ fallbacks within one point cloud")
        for node, kind in ((source, "point"), (hair, "curve")):
            rop.parm("soppath").set(node.path())
            frame = capture(session)
            expect(frame, ("red", "blue", "green", "green"))
            if kind == "curve":
                reference_status(session)
                session.write_exr(destination / (prefix + "-curves.exr"))
            status = session.status()
            conversions = status["geometry_conversions"]
            scans = status["material_binding_scans"]
            for iteration in range(12):
                gain = 0.5 if iteration % 2 else 1.0
                builders["blue"].node("surface_shader").parm("strength").set(gain)
                frame = capture(session)
                sample(frame, 1, np.multiply(colors["blue"], gain))
                assert (
                    session.status()["geometry_conversions"] == conversions
                    and session.status()["material_binding_scans"] == scans
                )
            builders["blue"].node("surface_shader").parm("strength").set(1.0)
            attribute = builders["blue"].createNode("cycles::attribute::1", "strand_Cd")
            attribute.parm("attribute").set("Cd")
            builders["blue"].node("surface_shader").setInput(0, attribute, 0)
            sample(capture(session), 1, (0.5, 0.25, 1.0))
            attribute_status = session.status()
            assert (
                attribute_status["geometry_conversions"] == conversions
                and attribute_status["material_binding_scans"] == scans
            ), (kind, status, attribute_status)
            builders["blue"].node("surface_shader").setInput(0, None)
            attribute.destroy()
            expect(capture(session), ("red", "blue", "green", "green"))
            checks.append(kind + " palette shader attribute demand reads owned Cd without host geometry conversion")
            camera.parm("tx").set(0.01)
            capture(session)
            camera.parm("tx").set(0)
            obj.parm("tx").set(0.01)
            capture(session)
            obj.parm("tx").set(0)
            assert (
                session.status()["geometry_conversions"] == conversions
                and session.status()["material_binding_scans"] == scans
            )
            node.parm("order").set("[3,1,0,2]")
            expect(capture(session), ("red", "blue", "green", "green"))
            node.parm("detailmat").set(builders["blue"].path())
            expect(capture(session), ("red", "blue", "blue", "blue"))
            node.parm("detailmat").set("")
            node.parm("order").set("[0,1,2,3]")
            checks.append(
                kind
                + " palette, repeated graph edits, camera/transform retention, identity reorder and detail precedence"
            )
            node.parm("offset").setExpression("$T * 2.4", hou.exprLanguage.Hscript)
            node.parm("reorder_motion").set(1)
            rop.parm("allowmotionblur").set(1)
            rop.parm("geometry_motion").set("deformation")
            expect(capture(session), ("red", "blue", "green", "green"))
            cached = session.status()
            builders["green"].setName("strand_green_renamed")
            obj.parm("shop_materialpath").set(builders["green"].path())
            expect(capture(session), ("red", "blue", "green", "green"))
            renamed = session.status()
            assert renamed["geometry_conversions"] == cached["geometry_conversions"] and renamed["motion_cooks"] == 0, (
                kind,
                {
                    k: (cached[k], renamed[k])
                    for k in (
                        "geometry_conversions",
                        "motion_cooks",
                        "material_binding_scans",
                        "extractions",
                        "cache_hits",
                    )
                },
            )
            assert any(r["path"] == builders["green"].path() for r in session.native_material_references())
            builders["green"].setName("strand_green")
            obj.parm("shop_materialpath").set(builders["green"].path())
            node.parm("animated_binding").set(1)
            expect(capture(session), ("red", "blue", "green", "green"))
            assert not session.status()["source_diagnostics"], session.status()
            node.parm("animated_binding").set(0)
            expect(capture(session), ("red", "blue", "green", "green"))
            rop.parm("geometry_motion").set("velocity")
            expect(capture(session), ("red", "blue", "green", "green"))
            checks.append(
                kind
                + " identity-aligned deformation palettes, cached fallback rename, nominal surface shading with animated bindings and velocity snapshot"
            )
            rop.parm("allowmotionblur").set(0)
            node.parm("offset").deleteAllKeyframes()
            node.parm("offset").set(0)
            node.parm("reorder_motion").set(0)
        rop.parm("soppath").set(source.path())
        source.parm("hide_first").set(1)
        expect(capture(session), ("red", "blue", "green", "green"))
        assert session.status()["points"] == 4
        source.parm("hide_first").set(0)
        checks.append("Simulation dead attribute does not hide points or alter shader slots")
        rop.parm("soppath").set(point_assignment.path())
        expect(capture(session), ("red", "blue", "blue", "green"))
        assert point_assignment.geometry().findPointAttrib("shop_materialpath") is not None
        checks.append("Actual Material SOP point-owner groups drive sphere shaders")
        source.parm("bindings").set(
            json.dumps(["", builders["green"].path(), "", "/mat/unused_missing_strand_material"])
        )
        obj.parm("shop_materialpath").set("/mat/unused_missing_strand_fallback")
        rop.parm("soppath").set(primitive_assignment.path())
        geometry = primitive_assignment.geometry()
        assert len(geometry.prims()) == 2
        assert len(geometry.points()[1].vertices()) == 2 and not geometry.points()[3].vertices()
        expect(capture(session), ("red", "green", "blue", None))
        assert session.status()["points"] == 3
        assert all("unused_missing" not in path for path in session.native_material_paths())
        source.parm("bindings").set(json.dumps(["", "", "", "/mat/unused_missing_strand_material"]))
        expect(recovery(session), ("red", "red", "blue", None))
        assert any(
            d["code"] == "material.conflicting_particle_binding" for d in session.status()["source_diagnostics"]
        ), session.status()
        source.parm("bindings").set(
            json.dumps(["", builders["green"].path(), "", "/mat/unused_missing_strand_material"])
        )
        expect(capture(session), ("red", "green", "blue", None))
        checks.append(
            "Point > containing particle primitive; overlapping owners, unused loose points/fallbacks, conflict recovery"
        )
        rop.parm("materialpath").set(builders["green"].path())
        expect(capture(session), ("green", "green", "green", None))
        assert not session.native_material_references()
        rop.parm("materialpath").set("")
        reset()
        set_rop_devices(rop, device)
        for node, code, fragment in (
            (
                source,
                "g.addAttrib(hou.attribType.Point,'material_override','');g.points()[0].setAttribValue('material_override',\"{'strength':2}\")",
                "point material overrides",
            ),
            (
                hair,
                "g.addAttrib(hou.attribType.Point,'shop_materialpath','');g.points()[0].setAttribValue('shop_materialpath','/mat/strand_blue')",
                "Varying point material bindings",
            ),
            (
                hair,
                "g.addAttrib(hou.attribType.Vertex,'shop_materialpath','');g.prims()[0].vertices()[0].setAttribValue('shop_materialpath','/mat/strand_blue')",
                "Varying vertex material bindings",
            ),
            (
                source,
                "g.findPointAttrib('shop_materialpath').destroy();g.addAttrib(hou.attribType.Point,'shop_materialpath',1)",
                "scalar string attribute",
            ),
        ):
            rop.parm("soppath").set(node.path())
            capture(session)
            node.parm("decorator").set(code)
            recovery(session)
            rop.parm("use_material_assignments").set(0)
            expect(capture(session), ("fallback",) * 4)
            node.parm("decorator").set("")
            rop.parm("use_material_assignments").set(1)
            expect(capture(session), ("red", "blue", "green", "green"))
        checks.append(
            "Unsupported point overrides, hair per-key/vertex bindings and malformed source attribute types warn while retaining renderable geometry"
        )
        for curves in (False, True):
            prototype.parm("hair").set(curves)
            prototype.parm("fully_bound").set(0)
            packed.parm("shop_materialpath1").set(builders["red"].path())
            packed.parm("shop_materialpath2").set(builders["green"].path())
            rop.parm("soppath").set(packed.path())
            frame = capture(session)
            pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(96, 96, 4)
            for x, name in ((-1.0, "blue"), (-0.6, "red"), (0.6, "blue"), (1.0, "green")):
                assert np.allclose(pixels[48, int((x / 4 + 0.5) * 96), :3], colors[name], atol=0.001, rtol=0), (
                    curves,
                    x,
                    pixels[48, int((x / 4 + 0.5) * 96), :3],
                )
            assert session.status()["geometry_resources"] == 2 and session.status()["object_resources"] == 2
            variant_bytes = session.status()["owned_geometry_bytes"]
            prototype.parm("fully_bound").set(1)
            frame = capture(session)
            assert (
                session.status()["geometry_resources"] == 1
                and session.status()["owned_geometry_bytes"] * 2 == variant_bytes
            )
            packed.parm("shop_materialpath1").set(builders["red"].path())
            packed.parm("shop_materialpath2").set(builders["red"].path())
            conversions = session.status()["geometry_conversions"]
            capture(session)
            assert (
                session.status()["geometry_conversions"] == conversions and session.status()["geometry_resources"] == 1
            )
            assert (
                len(session.native_material_references()) == 1
                and session.native_material_references()[0]["path"] == builders["blue"].path()
            )
            checks.append(
                ("curve" if curves else "point")
                + " packed palette variants, complete assignment sharing and unused fallback edits"
            )
        reset()
        set_rop_devices(rop, device)
        warm = []
        for iteration in range(20):
            rop.parm("soppath").set(source.path() if iteration % 2 else hair.path())
            capture(session)
            status = session.status()
            if iteration >= 8:
                warm.append(
                    (
                        iteration % 2,
                        status["device_allocation_bytes"],
                        status["owned_geometry_bytes"],
                        status["engine_shader_resources"],
                    )
                )
        for phase in (0, 1):
            assert len({tuple(row[1:]) for row in warm if row[0] == phase}) == 1, warm
        checks.append(dict(cross_kind_retirement="Stable warm allocation phases", warm=warm))
        reports.append(dict(device=device, result="passed", checks=checks, initial=initial, final=session.status()))
    assert session.status()["device_allocation_bytes"] == 0
reset()
set_rop_devices(rop, requested_device)
source.setRenderFlag(True)
scene = destination / "native-strand-materials.hiplc"
hou.hipFile.save(str(scene))
hou.hipFile.load(str(scene), suppress_save_prompt=True)
rop = hou.node("/out/native_strand_assignment_cycles")
with ch.Session(device=requested_device, width=96, height=96, samples=8) as session:
    expect(capture(session), ("red", "blue", "green", "green"))
report = dict(result="passed", device=requested_device, scene=str(scene), reports=reports)
(destination / "houdini-strand-material-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "houdini-strand-material-smoke.json")
