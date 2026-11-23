# SPDX-License-Identifier: Apache-2.0
"""Independent native binding images, packed variants, failures and retirement."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/assignments"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
colors = {"red": (0.8, 0.1, 0.05), "blue": (0.05, 0.1, 0.8), "green": (0.05, 0.7, 0.1), "fallback": (0.4, 0.1, 0.5)}
builders = {}
for name in ("red", "blue", "green"):
    builder = ch.create_material(name="assigned_" + name, shader="emission")
    shader = builder.node("surface_shader")
    shader.parmTuple("color").set(colors[name])
    shader.parm("strength").set(1.0)
    builders[name] = builder

obj = hou.node("/obj").createNode("geo", "assignment_receiver", run_init_scripts=False)
source = obj.createNode("python", "four_quads")
source.parm("python").set("""import hou
g = hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'id',0)
for cx,cy in [(-1,-1),(1,-1),(-1,1),(1,1)]:
    polygon = g.createPolygon()
    for dx,dy in [(-.7,-.7),(.7,-.7),(.7,.7),(-.7,.7)]:
        point=g.createPoint();point.setPosition((cx+dx,cy+dy,0));point.setAttribValue('id',len(g.points())-1);polygon.addVertex(point)
""")
assignment = obj.createNode("material", "primitive_materials")
assignment.setInput(0, source)
assignment.parm("num_materials").set(2)
for slot, name in ((1, "red"), (2, "blue")):
    assignment.parm(f"group{slot}").set(str(slot - 1))
    assignment.parm(f"shop_materialpath{slot}").set(builders[name].path())
detail = obj.createNode("python", "detail_fallback")
detail.setInput(0, assignment)
detail.addSpareParmTuple(hou.StringParmTemplate("detailmat", "Detail Material", 1, default_value=("",)))
detail.addSpareParmTuple(hou.StringParmTemplate("decorator", "Test Attributes", 1, default_value=("",)))
detail.parm("python").set("""import hou
node=hou.pwd();g=node.geometry();path=node.evalParm('detailmat')
if path:
    g.addAttrib(hou.attribType.Global,'shop_materialpath','');g.setGlobalAttribValue('shop_materialpath',path)
exec(node.evalParm('decorator'))
""")
detail.setRenderFlag(True)
obj.parm("shop_materialpath").set(builders["green"].path())
camera = hou.node("/obj").createNode("cam", "assignment_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr", "native_assignment_cycles")
rop.parm("source_mode").set("sop")
rop.setParms(dict(use_denoise=0, ipr_use_denoise=0))
set_rop_devices(rop, "CPU")
for name, value in dict(
    soppath=detail.path(), camera=camera.path(), resx=96, resy=96, samples=8, material_kind="emission"
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor").set(colors["fallback"])
rop.parm("emission_strength").set(1.0)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
assert rop.evalParm("use_material_assignments") == 1 and not rop.evalParm("materialpath")
assert len(detail.geometry().prims()) == 4
assert [prim.attribValue("shop_materialpath") for prim in detail.geometry().prims()] == [
    builders["red"].path(),
    builders["blue"].path(),
    "",
    "",
]


def pixels(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(
        frame["height"], frame["width"], 4
    )


def sample(frame, x, y=0, expected=None):
    ix, iy = round((x / 4 + 0.5) * frame["width"]), round((y / 4 + 0.5) * frame["height"])
    actual = pixels(frame)[iy - 1 : iy + 2, ix - 1 : ix + 2, :3].mean(axis=(0, 1))
    if expected is not None:
        assert np.allclose(actual, expected, atol=0.001, rtol=0), (x, y, actual, expected)
    return actual.tolist()


def quadrants(frame, names):
    return [sample(frame, x, y, colors[name]) for (x, y), name in zip(((-1, -1), (1, -1), (-1, 1), (1, 1)), names)]


def capture(session, at_time=None):
    now = hou.time() if at_time is None else at_time
    return session.wait(session.apply(native_scene.capture(rop, now), at_time=now))


def recovery(session, action, fragment):
    result = action()
    if isinstance(result, int):
        session.wait(result)
    status = session.status()
    assert not status["error"] and status["state"] == "complete", status
    assert status["source_diagnostics"], status
    return dict(recovered=fragment, diagnostics=status["source_diagnostics"])


def rejection(session, action, fragment):
    before = session.status()
    generation = before.get("committed_generation", before["generation"])
    last = session.frame_info()
    try:
        result = action()
        if isinstance(result, int):
            session.wait(result)
    except (RuntimeError, ValueError) as error:
        assert fragment in str(error), (fragment, str(error))
        message = str(error)
    else:
        raise AssertionError("Invalid native material update was accepted: " + fragment)
    rejected = session.status()
    assert rejected.get("committed_generation", rejected["generation"]) == generation
    assert session.frame_info()["generation"] == last["generation"]
    return message


# Packed source has two quads within each prototype. The left is independently
# bound to blue; the right inherits the packed primitive's fallback.
packed_obj = hou.node("/obj").createNode("geo", "assignment_packed", run_init_scripts=False)
prototype = packed_obj.createNode("python", "prototype_quads")
prototype.parm("python").set("""import hou
g=hou.pwd().geometry()
for cx in [-.22,.22]:
    p=g.createPolygon()
    for dx,dy in [(-.18,-.45),(.18,-.45),(.18,.45),(-.18,.45)]:
        v=g.createPoint();v.setPosition((cx+dx,dy,0));p.addVertex(v)
""")
embedded = packed_obj.createNode("material", "embedded_material")
embedded.setInput(0, prototype)
embedded.parm("group1").set("0")
embedded.parm("shop_materialpath1").set(builders["blue"].path())
points = packed_obj.createNode("python", "instance_points")
points.parm("python").set("""import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'id',0)
for identity,x in [(10,-.9),(20,.9)]:
    p=g.createPoint();p.setPosition((x,0,0));p.setAttribValue('id',identity)
""")
copies = packed_obj.createNode("copytopoints", "copies")
copies.setInput(0, embedded)
copies.setInput(1, points)
copies.parm("pack").set(1)
packed = packed_obj.createNode("python", "instance_materials")
packed.setInput(0, copies)
for name in ("firstmat", "secondmat"):
    packed.addSpareParmTuple(hou.StringParmTemplate(name, name, 1, default_value=("",)))
packed.parm("python").set("""import hou
node=hou.pwd();g=node.geometry();g.addAttrib(hou.attribType.Prim,'shop_materialpath','');g.addAttrib(hou.attribType.Prim,'id',0)
for i,p in enumerate(g.prims()):
    p.setAttribValue('shop_materialpath',node.evalParm('firstmat' if i==0 else 'secondmat'));p.setAttribValue('id',10+10*i)
""")
packed.parm("firstmat").set(builders["red"].path())
packed.parm("secondmat").set(builders["green"].path())
packed.setRenderFlag(True)
packed_obj.parm("shop_materialpath").set("/mat/unused_missing_object_material")

fx_obj = hou.node("/obj").createNode("geo", "assignment_boundaries", run_init_scripts=False)
point_source = fx_obj.createNode("python", "sphere")
point_code = "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'pscale',.25);p=g.createPoint();p.setPosition((0,0,0))"
point_source.parm("python").set(point_code)
curve_source = fx_obj.createNode("python", "curve")
curve_code = "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'width',.3);c=g.createPolygon(is_closed=False)\nfor x in (-.5,.5):\n    p=g.createPoint();p.setPosition((x,0,0));c.addVertex(p)"
curve_source.parm("python").set(curve_code)
fx_obj.parm("shop_materialpath").set(builders["green"].path())


def reset_fixture():
    hou.setFrame(1)
    camera.parm("tx").set(0)
    obj.parm("tx").set(0)
    obj.parm("shop_materialpath").set(builders["green"].path())
    assignment.parm("shop_materialpath1").deleteAllKeyframes()
    assignment.parm("shop_materialpath1").set(builders["red"].path())
    assignment.parm("shop_materialpath2").set(builders["blue"].path())
    detail.parm("detailmat").set("")
    detail.parm("decorator").set("")
    embedded.parm("group1").set("0")
    embedded.parm("shop_materialpath1").set(builders["blue"].path())
    packed.parm("firstmat").set(builders["red"].path())
    packed.parm("secondmat").set(builders["green"].path())
    rop.parm("soppath").set(detail.path())
    rop.parm("materialpath").set("")
    rop.parm("use_material_assignments").set(1)
    rop.parm("allowmotionblur").set(0)
    rop.parm("geometry_motion").set("transform")
    for name, builder in builders.items():
        builder.node("surface_shader").parmTuple("color").set(colors[name])
        builder.node("surface_shader").parm("strength").deleteAllKeyframes()
        builder.node("surface_shader").parm("strength").set(1.0)


reports = []
for device in ("CPU", ch.default_device()):
    reset_fixture()
    set_rop_devices(rop, device)
    checks = []
    with ch.Session(device=device, width=96, height=96, samples=8, collect_diagnostics=True) as session:
        frame = capture(session)
        initial = session.status()
        expected = quadrants(frame, ("red", "blue", "green", "green"))
        references = session.native_material_references()
        assert {r["key"] for r in references} == {
            f"native-material:{b.userData('cycles_graph_id')}:{b.sessionId()}" for b in builders.values()
        }
        assert all(hou.node(r["path"]).sessionId() == r["session_id"] for r in references)
        assert (
            initial["geometry_resources"] == initial["object_resources"] == 1 and initial["material_resources"] == 4
        ), initial
        checks.append("Material SOP subsets, empty primitive fallback, OBJ material, and HDK/HOM lifetime identity")

        scans = initial["material_binding_scans"]
        conversions = initial["geometry_conversions"]
        for iteration in range(20):
            gain = 0.5 if iteration % 2 else 1.0
            builders["red"].node("surface_shader").parm("strength").set(gain)
            frame = capture(session)
            sample(frame, -1, -1, np.multiply(colors["red"], gain))
            assert session.status()["geometry_conversions"] == conversions
            assert session.status()["material_binding_scans"] == scans
        builders["red"].node("surface_shader").parm("strength").set(1.0)
        camera.parm("tx").set(0.05)
        capture(session)
        camera.parm("tx").set(0)
        obj.parm("tx").set(0.05)
        capture(session)
        obj.parm("tx").set(0)
        assert (
            session.status()["geometry_conversions"] == conversions
            and session.status()["material_binding_scans"] == scans
        )
        checks.append("Repeated shader, camera and transform edits reuse geometry and binding-table scans")

        detail.parm("detailmat").set(builders["red"].path())
        obj.parm("shop_materialpath").set("/mat/unused_missing")
        quadrants(capture(session), ("red", "blue", "red", "red"))
        assert {r["path"] for r in session.native_material_references()} == {
            builders["red"].path(),
            builders["blue"].path(),
        }
        assert session.status()["material_resources"] == 3
        detail.parm("detailmat").set("")
        obj.parm("shop_materialpath").set("op:" + builders["blue"].path())
        assignment.parm("shop_materialpath2").set("op:" + builders["blue"].path())
        quadrants(capture(session), ("red", "blue", "blue", "blue"))
        assert len(session.native_material_references()) == 2
        obj.parm("shop_materialpath").set(obj.relativePathTo(builders["green"]))
        quadrants(capture(session), ("red", "blue", "green", "green"))
        checks.append("Detail precedence, unused missing OBJ fallback, op: aliases and relative OBJ paths")

        # Keep both native resources present while changing only which one the
        # unassigned faces inherit. A shader-key set and unchanged GU ids alone
        # cannot identify the resulting per-face slot assignment.
        assignment.parm("shop_materialpath1").set(builders["green"].path())
        assignment.parm("shop_materialpath2").set(builders["blue"].path())
        obj.parm("shop_materialpath").set(builders["green"].path())
        quadrants(capture(session), ("green", "blue", "green", "green"))
        referenced = {r["key"] for r in session.native_material_references()}
        obj.parm("shop_materialpath").set(builders["blue"].path())
        inherited = capture(session)
        actual = quadrants(inherited, ("green", "blue", "blue", "blue"))
        assert {r["key"] for r in session.native_material_references()} == referenced
        with ch.Session(device=device, width=96, height=96, samples=8, collect_diagnostics=True) as fresh:
            expected = quadrants(capture(fresh), ("green", "blue", "blue", "blue"))
        assert np.allclose(actual, expected, rtol=0, atol=0.001)
        checks.append("OBJ fallback edit among existing shader slots matches a fresh renderer")
        reset_fixture()

        # Invalid source assignments use local fallbacks; unsupported layers are
        # ignored without rejecting the render.
        errors = []
        # Material SOP deliberately omits nonexistent operator paths and emits
        # a host warning. Inject actual stale asset strings to test the adapter.
        detail.parm("decorator").set("g.prim(0).setAttribValue('shop_materialpath','/mat/missing_assignment')")
        errors.append(recovery(session, lambda: capture(session), "Missing Cycles material"))
        assert "/mat/missing_assignment" in session.native_material_paths()
        foreign = hou.node("/mat").createNode("principledshader", "foreign_assignment")
        detail.parm("decorator").set(f"g.prim(0).setAttribValue('shop_materialpath',{foreign.path()!r})")
        errors.append(recovery(session, lambda: capture(session), "Cycles Material builder"))
        detail.parm("decorator").set(
            "g.prim(0).setAttribValue('shop_materialpath','/mat/missing_assignment')\n"
            + f"g.prim(1).setAttribValue('shop_materialpath',{foreign.path()!r})"
        )
        mixed = capture(session)
        sample(mixed, -1, -1, (1, 0, 1))
        sample(mixed, 1, -1, (1, 0, 1))
        sample(mixed, -1, 1, colors["green"])
        sample(mixed, 1, 1, colors["green"])
        assert len(session.status()["source_diagnostics"]) == 2
        checks.append(
            "Missing and foreign face assignments are marked independently while valid faces keep their shader"
        )
        detail.parm("decorator").set("g.prim(0).setAttribValue('shop_materialpath','../assigned_red')")
        errors.append(recovery(session, lambda: capture(session), "absolute operator paths"))
        detail.parm("decorator").set("")
        for owner in ("Prim", "Global"):
            for name, value in (("material_override", "{'strength': 2}"), ("material_stylesheet", '{"styles":[{}]}')):
                setter = "p.setAttribValue(name,value)" if owner == "Prim" else "g.setGlobalAttribValue(name,value)"
                loop = "for p in g.prims():\n    " + setter if owner == "Prim" else setter
                detail.parm("decorator").set(
                    f"name={name!r};value={value!r}\ng.addAttrib(hou.attribType.{owner},name,'')\n" + loop
                )
                if name == "material_stylesheet":
                    capture(session)
                    assert not session.status()["source_diagnostics"]
                    checks.append("Empty native stylesheet rules retain bindings without warnings")
                else:
                    errors.append(recovery(session, lambda: capture(session), name))
                quadrants(session.frame(), ("red", "blue", "green", "green"))
        detail.parm("decorator").set(
            "g.findPrimAttrib('shop_materialpath').destroy();g.addAttrib(hou.attribType.Prim,'shop_materialpath',0)"
        )
        errors.append(recovery(session, lambda: capture(session), "scalar string"))
        detail.parm("decorator").set(
            "g.addAttrib(hou.attribType.Prim,'material_override','{}');g.addAttrib(hou.attribType.Global,'material_stylesheet','{}')"
        )
        quadrants(capture(session), ("red", "blue", "green", "green"))
        detail.parm("decorator").set("")
        foreign.destroy()
        detail.parm("decorator").set(
            "g.addAttrib(hou.attribType.Prim,'unrelated_asset_path','/missing/unrelated');g.addAttrib(hou.attribType.Point,'unused_float',float('nan'))"
        )
        quadrants(capture(session), ("red", "blue", "green", "green"))
        assert not session.status()["source_diagnostics"]
        detail.parm("decorator").set("")
        checks.append("Irrelevant missing-path and non-finite attributes are ignored quietly")
        checks.append(dict(transaction_failures=errors))

        # Resolver arguments are owned dictionaries; exact key coverage is
        # validated, exceptions retain their meaning, and reentry is rejected.
        valid = capture(session)
        for resolver, fragment in (
            (lambda refs, t: [], "omitted"),
            (
                lambda refs, t: [dict(key="foreign", kind="graph", graph=ch.material_graph(builders["red"]))],
                "exactly one",
            ),
        ):
            update = native_scene.capture(rop)
            update["material_resolver"] = resolver
            rejection(session, lambda: session.apply(update), fragment)

        def bad_resolver(refs, t):
            raise ValueError("Deliberate material callback failure")

        update = native_scene.capture(rop)
        update["material_resolver"] = bad_resolver
        rejection(session, lambda: session.apply(update), "Deliberate material callback failure")
        update = native_scene.capture(rop)
        update["material_resolver"] = lambda refs, t: session.apply({})
        rejection(session, lambda: session.apply(update), "cannot be reentered")
        update = native_scene.capture(rop)
        update["material_resolver"] = lambda refs, t: session.close()
        rejection(session, lambda: session.apply(update), "Cannot close")
        capture(session)
        session.wait(session.apply({"crop": (0, 0, 96, 96)}))
        assert len(session.native_material_references()) == 3
        checks.append("Resolver exact coverage, propagated exceptions, transaction/close guards and crop-only updates")

        # A new builder at the identical path must replace the old shader even
        # when SOP geometry IDs and string contents are unchanged.
        old = builders["red"]
        old_path = old.path()
        old_id = old.sessionId()
        old_identity = old.userData("cycles_graph_id")
        old.destroy()
        replacement = ch.create_material(name=old_path.rsplit("/", 1)[1], shader="emission")
        replacement.node("surface_shader").parmTuple("color").set(colors["red"])
        replacement.node("surface_shader").parm("strength").set(1.0)
        builders["red"] = replacement
        quadrants(capture(session), ("red", "blue", "green", "green"))
        assert replacement.userData("cycles_graph_id") != old_identity
        assert f"native-material:{old_identity}:{old_id}" not in {
            r["key"] for r in session.native_material_references()
        }
        assert session.status()["material_resources"] == 4
        checks.append("Delete/recreate at the same operator path replaces stale material lifetime IDs")

        # Shading uses the requested-frame assignment while geometry retains
        # valid deformation correspondence across changing face bindings.
        rop.parm("allowmotionblur").set(1)
        rop.parm("geometry_motion").set("deformation")
        quadrants(capture(session), ("red", "blue", "green", "green"))
        assignment.parm("shop_materialpath1").setExpression(
            f'ifs($T < 0, "{builders["red"].path()}", "{builders["blue"].path()}")', hou.exprLanguage.Hscript
        )
        quadrants(capture(session), ("blue", "blue", "green", "green"))
        assert not session.status()["source_diagnostics"] and not session.status()["motion_warnings"]
        assignment.parm("shop_materialpath1").deleteAllKeyframes()
        assignment.parm("shop_materialpath1").set(builders["red"].path())
        rop.parm("allowmotionblur").set(0)
        rop.parm("geometry_motion").set("transform")
        quadrants(capture(session), ("red", "blue", "green", "green"))
        checks.append("Requested-frame shading across animated bindings retains valid deformation")

        rop.parm("soppath").set(packed.path())
        frame = capture(session)
        packed_expected = [
            sample(frame, x, 0, colors[name])
            for x, name in ((-1.12, "blue"), (-0.68, "red"), (0.68, "blue"), (1.12, "green"))
        ]
        assert session.status()["geometry_resources"] == 2 and session.status()["object_resources"] == 2
        assert session.status()["mesh_variant_resources"] == 1, session.status()
        variant_bytes = session.status()["owned_geometry_bytes"]
        identities = session.native_object_keys()
        packed.parm("secondmat").set(builders["red"].path())
        capture(session)
        assert session.status()["geometry_resources"] == 1 and session.status()["mesh_variant_resources"] == 1
        assert session.status()["owned_geometry_bytes"] < variant_bytes
        assert session.native_object_keys() == identities
        packed.parm("secondmat").set(builders["green"].path())
        embedded.parm("group1").set("")
        frame = capture(session)
        for x in (-1.12, -0.68, 0.68, 1.12):
            sample(frame, x, 0, colors["blue"])
        assert session.status()["geometry_resources"] == 1
        packed.parm("firstmat").set("/mat/unused_missing_packed")
        packed.parm("secondmat").set("/mat/another_unused_missing")
        conversions = session.status()["geometry_conversions"]
        frame = capture(session)
        assert session.status()["geometry_resources"] == 1 and session.status()["geometry_conversions"] == conversions
        assert [r["path"] for r in session.native_material_references()] == [builders["blue"].path()]
        assert session.status()["material_resources"] == 2
        checks.append(
            dict(
                packed_precedence="embedded > packed primitive > OBJ; unused fallbacks ignored",
                independent_pixels=packed_expected,
                variants=2,
                shared_variants=1,
                variant_bytes=variant_bytes,
            )
        )

        # Different outer fallback keys also cannot split fully bound instances.
        update = native_scene.capture(rop)
        first = update["geometry"][0]
        second = dict(
            first,
            object_key="second_native_group",
            material="other_fallback",
            object_material_path="/mat/missing_other",
        )
        second["transform"] = list(first["transform"])
        second["transform"][7] = 1.1
        update["geometry"].append(second)
        update["materials"].append(dict(key="other_fallback", kind="emission", color=(0, 0, 0)))
        session.wait(session.apply(update))
        assert session.status()["geometry_resources"] == 1 and session.status()["object_resources"] == 4
        capture(session)
        embedded.parm("group1").set("0")
        packed.parm("firstmat").set(builders["red"].path())
        packed.parm("secondmat").set(builders["green"].path())
        nested = packed_obj.createNode("pack", "nested_material_pair")
        nested.setInput(0, packed)
        outer = packed_obj.createNode("material", "outer_unused_material")
        outer.setInput(0, nested)
        outer.parm("shop_materialpath1").set("/mat/unused_nested_material")
        rop.parm("soppath").set(outer.path())
        frame = capture(session)
        for x, name in ((-1.12, "blue"), (-0.68, "red"), (0.68, "blue"), (1.12, "green")):
            sample(frame, x, 0, colors[name])
        assert session.status()["geometry_resources"] == 2 and session.status()["object_resources"] == 2
        checks.append("Cross-request sharing and nested packed fallback propagation")

        # Whole-source ROP override is intentional and must ignore all embedded
        # assignment/override attributes, including foreign missing paths.
        rop.parm("materialpath").set(builders["green"].path())
        frame = capture(session)
        for x in (-1.12, -0.68, 0.68, 1.12):
            sample(frame, x, 0, colors["green"])
        assert session.status()["geometry_resources"] == 1 and not session.native_material_references()
        rop.parm("materialpath").set("")
        rop.parm("use_material_assignments").set(0)
        frame = capture(session)
        for x in (-1.12, -0.68, 0.68, 1.12):
            sample(frame, x, 0, colors["fallback"])
        assert (
            session.status()["material_resources"] == 2
        )  # One intentional low-level 'other_fallback' remains caller-owned.
        checks.append("Explicit ROP graph override and uniform controls ignore native assignments")
        nested.destroy()
        outer.destroy()

        rop.parm("materialpath").set("")
        rop.parm("use_material_assignments").set(1)
        for source_node, code, owner in ((point_source, point_code, "Point"), (curve_source, curve_code, "Prim")):
            rop.parm("soppath").set(source_node.path())
            sample(capture(session), 0, 0, colors["green"])
            elements = "points" if owner == "Point" else "prims"
            path = builders["blue"].path()
            source_node.parm("python").set(
                code
                + f"\ng.addAttrib(hou.attribType.{owner},'shop_materialpath','')\nfor e in g.{elements}(): e.setAttribValue('shop_materialpath',{path!r})"
            )
            assert all(e.attribValue("shop_materialpath") == path for e in getattr(source_node.geometry(), elements)())
            sample(capture(session), 0, 0, colors["blue"])
            source_node.parm("python").set(code)
            sample(capture(session), 0, 0, colors["green"])
        checks.append("OBJ surface graphs and per-point/per-curve bindings render on spheres and hair")

        reset_fixture()
        set_rop_devices(rop, device)
        capture(session)
        warm = []
        pool_counts = []
        for iteration in range(24):
            # Retire and recreate native material uses without destroying host
            # builders. Bounded GPU/resource state is checked after warm-up.
            assignment.parm("shop_materialpath2").set(builders["blue" if iteration % 2 else "red"].path())
            capture(session)
            state = session.status()
            assert state["material_resources"] in (4, 5)  # Three/two native + ROP + caller-owned fallback.
            if iteration >= 8:
                warm.append((iteration % 2, state["device_allocation_bytes"], state["owned_geometry_bytes"]))
                pool_counts.append((state["engine_shader_resources"], state["retired_shader_resources"]))
        for phase in (0, 1):
            values = {(allocation, owned) for parity, allocation, owned in warm if parity == phase}
            assert len(values) == 1, ("Retained allocation grew during native material retirement", phase, values)
        assert session.status()["shader_reuses"] >= 8, session.status()
        assert (
            len({total for total, retired in pool_counts}) == 1 and max(retired for total, retired in pool_counts) > 0
        ), pool_counts
        reset_fixture()
        set_rop_devices(rop, device)
        capture(session)
        session.write_exr(destination / ("assignment-cpu.exr" if device == "CPU" else "assignment-optix.exr"))
        session.wait(session.apply(dict(geometry=[])))
        assert session.status()["geometry_resources"] == session.status()["object_resources"] == 0
        assert not session.native_material_references() and session.status()["material_resources"] == 2
        checks.append(
            dict(
                retirement="Native shaders retire with their geometry; manual materials remain caller-owned",
                warm_allocations=warm,
                shader_pool_counts=pool_counts,
            )
        )
        reports.append(
            dict(
                device=device,
                result="passed",
                checks=checks,
                initial=initial,
                independent_quadrants=expected,
                final=session.status(),
            )
        )
    assert session.status()["device_allocation_bytes"] == 0 and not session.native_material_paths()
    assert session.status()["engine_shader_resources"] == session.status()["retired_shader_resources"] == 0

# Observe only referenced graph parameters; watch nearest existing networks for
# failed-path recovery, and replace stale dependency sets after reassignment.
reset_fixture()
observations = []
unused = ch.create_material(name="unused_assignment_graph", shader="emission")
# The default this test was written for (new nodes now take Blender's).
unused.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8)))
with ch.Session(device="CPU", width=96, height=96, samples=8) as observer_session:
    capture(observer_session)
    observer = native_scene.SceneObserver(
        rop, lambda: observations.append("changed"), observer_session.native_material_paths
    )
    try:
        builders["blue"].node("surface_shader").parm("strength").set(1.1)
        assert observations
        observations.clear()
        unused.node("surface_shader").parm("strength").set(2.0)
        assert not observations, "An unreferenced graph parameter was observed"
        detail.parm("decorator").set("g.prim(0).setAttribValue('shop_materialpath','/mat/recovered_assignment')")
        recovery(observer_session, lambda: capture(observer_session), "Missing Cycles material")
        observer.refresh()
        observations.clear()
        recovered = ch.create_material(name="recovered_assignment", shader="emission")
        recovered.node("surface_shader").parmTuple("color").set(colors["red"])
        recovered.node("surface_shader").parm("strength").set(1.0)
        assert observations, "Missing-path parent creation did not trigger recovery"
        capture(observer_session)
        observer.refresh()
        observations.clear()
        recovered.node("surface_shader").parm("strength").set(1.2)
        assert observations
        observations.clear()
        detail.parm("decorator").set("")
        capture(observer_session)
        observer.refresh()
        observations.clear()
        recovered.node("surface_shader").parm("strength").set(1.3)
        assert not observations, "An obsolete graph dependency was retained"
    finally:
        observer.close()
unused.destroy()
recovered.destroy()

# The production orchestration re-extracts referenced graphs for each batch
# frame, independent of the interactive observer and current Houdini frame.
reset_fixture()
set_rop_devices(rop, "CPU")
builders["red"].node("surface_shader").parm("strength").setExpression("$F", hou.exprLanguage.Hscript)
rop.parm("picture").set(str(destination / "batch-assignment.$F4.exr"))
rop.render(frame_range=(1, 3, 1))
for frame in (1, 2, 3):
    image = OIIO.ImageInput.open(str(destination / f"batch-assignment.{frame:04d}.exr"))
    assert image
    spec = image.spec()
    data = image.read_image(format=OIIO.FLOAT)
    image.close()
    offset = spec.channelnames.index("R")
    # EXR rows run top to bottom; Houdini's independently specified lower-left
    # quadrant centre (24,24) is file row 95-24=71.
    actual = data[70:73, 23:26, offset : offset + 3].mean(axis=(0, 1))
    assert np.allclose(actual, np.multiply(colors["red"], frame), atol=0.001, rtol=0), (frame, actual)
assert hou.frame() == 1, "Explicit batch time moved the host frame"
builders["red"].node("surface_shader").parm("strength").deleteAllKeyframes()
builders["red"].node("surface_shader").parm("strength").set(1.0)
fixture = destination / "native-material-assignments.hiplc"
hou.hipFile.save(str(fixture))
paths = {name: builder.path() for name, builder in builders.items()}
expected_graphs = {name: ch.material_graph(builder) for name, builder in builders.items()}
hou.hipFile.load(str(fixture), suppress_save_prompt=True)
assert all(ch.material_graph(hou.node(path)) == expected_graphs[name] for name, path in paths.items())
report = dict(
    result="passed",
    fixtures=reports,
    saved_fixture=str(fixture),
    batch_frames=[1, 2, 3],
    observer="Referenced parameters only, nearest-parent missing-path recovery and obsolete dependency retirement passed",
    scope="Native OBJ/detail/primitive polygon bindings, packed/nested variants, transactional errors, shader updates and resource retirement; per-point/curve slots, overrides/style sheets and volume graphs remain deferred",
)
(destination / "houdini-material-assignment-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "houdini-material-assignment-smoke.json")
