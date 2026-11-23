# SPDX-License-Identifier: Apache-2.0
"""Point instancing end to end. An Instance OBJ scene must render like a reference scene
made of ordinary objects placed at the oracle's transforms (prototype.world . point . instancer.world)
with the materials Houdini's precedence predicts (point > prototype OBJ > instancer OBJ), the instance
Cd read by an Attribute node, ptinstance=off, an object with s@instance, and per-point Dielectric
Priority (engine read-back). Expectations come from the oracle before rendering. Generated scene only."""

import argparse, json, os, sys
from pathlib import Path
import numpy as np
import hou

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import point_instance_oracle as oracle
import pass_fixtures as pass_fixture
import cycles_houdini as ch
from cycles_houdini import native_scene, materials

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--out", default="")
args = parser.parse_args()
out = Path(args.out) if args.out else Path(".")
out.mkdir(parents=True, exist_ok=True)
W, H, SAMPLES = 160, 120, 16
results = []
failures = []


def check(name, ok, **details):
    results.append(dict(name=name, ok=bool(ok), **details))
    print(("ok: " if ok else "FAIL: ") + name, details if not ok else "", flush=True)
    if not ok:
        failures.append(name)


hou.setFrame(1)
now = hou.time()
root = hou.node("/obj")


def emission(name, color):
    m = materials.create_material(name=name, shader="emission")
    pass_fixture.set_parameters(m.node("surface_shader"), dict(strength=1))
    m.node("surface_shader").parmTuple("color").set(color)
    return m


RED, GREEN, BLUE = emission("red", (1, 0, 0)), emission("green", (0, 1, 0)), emission("blue", (0, 0, 1))
ch.promote_parameter(RED, RED.node("surface_shader"), "color", name="tint")  # For point material_override.
CYAN = emission("cyan_reference", (0, 1, 1))
ATTR = materials.create_material(name="from_cd", shader="emission")
pass_fixture.set_parameters(ATTR.node("surface_shader"), dict(strength=1))
field = ATTR.createNode("cycles::attribute::1", "cd")
pass_fixture.parm(field, "attribute").set("Cd")
ATTR.node("surface_shader").setNamedInput("color", field, "color")
# A shader reading a transform-named attribute (pscale) from the instance: it must be kept on the instances.
PSCALE = materials.create_material(name="from_pscale", shader="emission")
pass_fixture.set_parameters(PSCALE.node("surface_shader"), dict(strength=1))
pfield = PSCALE.createNode("cycles::attribute::1", "pscale")
pass_fixture.parm(pfield, "attribute").set("pscale")
PSCALE.node("surface_shader").setNamedInput("color", pfield, "fac")


def prototype(name, sop_type, t, r, s, material=None):
    node = root.createNode("geo", name)
    for child in node.children():
        child.destroy()
    shape = node.createNode(sop_type)
    shape.setRenderFlag(True)
    shape.setDisplayFlag(True)
    if sop_type == "sphere":
        shape.parm("type").set(1)
        shape.parmTuple("rad").set((0.35, 0.35, 0.35))
    if sop_type == "box":
        shape.parmTuple("size").set((0.6, 0.4, 0.3))
    node.parmTuple("t").set(t)
    node.parmTuple("r").set(r)
    node.parmTuple("s").set(s)
    if material:
        node.parm("shop_materialpath").set(material.path())
    node.setDisplayFlag(False)
    return node


protoA = prototype("protoA", "box", (0, 0.2, 0), (0, 25, 10), (1, 1.5, -1), RED)
protoB = prototype("protoB", "sphere", (0.1, 0, 0), (0, 0, 0), (1, 1, 1))
protoC = prototype("protoC", "box", (0, 0, 0), (0, 0, 0), (1, 1, 1), ATTR)
protoD = prototype("protoD", "box", (0, 0, 0), (0, 0, 0), (1, 1, 1), PSCALE)


def wrangle_object(kind, name, snippet, t=(0, 0, 0), r=(0, 0, 0)):
    node = root.createNode(kind, name)
    for child in node.children():
        child.destroy()
    w = node.createNode("attribwrangle", "points")
    w.parm("class").set(0)
    w.parm("snippet").set(snippet)
    w.setRenderFlag(True)
    w.setDisplayFlag(True)
    if kind == "instance":
        node.parmTuple("i_t").set(t)
        node.parmTuple("i_r").set(r)
    else:
        node.parmTuple("t").set(t)
        node.parmTuple("r").set(r)
    return node


inst = wrangle_object(
    "instance",
    "inst",
    """
addpointattrib(0,"pscale",1.0); addpointattrib(0,"scale",{1,1,1});
int a=addpoint(0,{-1.6,0,0}); setpointattrib(0,"instance",a,"../protoA"); setpointattrib(0,"orient",a,quaternion(radians(40),{0,0,1}));
int b=addpoint(0,{-0.5,0.4,0}); setpointattrib(0,"instance",b,"../protoB"); setpointattrib(0,"pscale",b,1.3);
int c=addpoint(0,{0.6,-0.4,0}); setpointattrib(0,"instance",c,"/obj/protoA"); setpointattrib(0,"shop_materialpath",c,"%s"); setpointattrib(0,"N",c,{1,0.3,0.2});
int d=addpoint(0,{1.6,0.3,0}); setpointattrib(0,"instance",d,"../protoC"); setpointattrib(0,"Cd",d,{0.9,0.6,0.1}); setpointattrib(0,"scale",d,{1,-1.4,0.8});
int e=addpoint(0,{1.6,-0.7,0}); setpointattrib(0,"instance",e,"../protoC"); setpointattrib(0,"Cd",e,{0.1,0.5,0.9});
int g=addpoint(0,{-1.7,1.1,0}); setpointattrib(0,"instance",g,"../protoD"); setpointattrib(0,"pscale",g,0.45);
int f=addpoint(0,{0,1.2,0}); setpointattrib(0,"instance",f,"../protoA"); setpointattrib(0,"material_override",f,"{'tintr':0,'tintg':1,'tintb':1}");
"""
    % BLUE.path(),
    t=(0, 0.1, 0),
    r=(0, 0, 5),
)
inst.parm("ptinstance").set("fast")
inst.parm("shop_materialpath").set(GREEN.path())
off = wrangle_object("instance", "inst_off", "int a=addpoint(0,{5,5,5});", t=(0, -1.1, 0), r=(0, 0, 0))
off.parm("instancepath").set("../protoA")
off.parm("ptinstance").set("off")
geo_attr = wrangle_object(
    "geo",
    "scatter",
    'int a=addpoint(0,{-1.2,1.1,0}); setpointattrib(0,"instance",a,"../protoB");'
    'int b=addpoint(0,{0,1.1,0}); setpointattrib(0,"instance",b,"../protoA");',
    t=(0, 0, 0),
)
geo_attr.parm("shop_materialpath").set(GREEN.path())
# A subnet prototype: its members are placed at their own world transforms.
assembly = root.createNode("subnet", "assembly")
assembly.parmTuple("t").set((0, 0, 0.2))
for i, (sop_type, offset) in enumerate((("box", (-0.25, 0, 0)), ("sphere", (0.25, 0.1, 0)))):
    member = assembly.createNode("geo", "member%d" % i)
    for child in member.children():
        child.destroy()
    shape = member.createNode(sop_type)
    shape.setRenderFlag(True)
    shape.setDisplayFlag(True)
    if sop_type == "sphere":
        shape.parm("type").set(1)
        shape.parmTuple("rad").set((0.15, 0.15, 0.15))
    else:
        shape.parmTuple("size").set((0.2, 0.3, 0.2))
    member.parmTuple("t").set(offset)
    member.parm("shop_materialpath").set((RED if i else BLUE).path())
assembly.setDisplayFlag(False)
subnet_inst = wrangle_object(
    "instance",
    "inst_subnet",
    'int a=addpoint(0,{-1.5,-1.2,0}); int b=addpoint(0,{1.4,-1.2,0}); setpointattrib(0,"orient",b,quaternion(radians(70),{0,0,1}));',
)
subnet_inst.parm("instancepath").set("../assembly")
subnet_inst.parm("ptinstance").set("fast")
subnet_inst.parm("subnet_filter").set("all")
cam = root.createNode("cam", "cam")
pass_fixture.set_parameters(cam, dict(tz=7, resx=W, resy=H))
cam.parm("projection").set("ortho")
cam.parm("orthowidth").set(5.5)


# Reference objects at oracle transforms with the predicted material and Cd.
def m4(values):
    v = list(values.asTuple())
    return [v[i * 4 : (i + 1) * 4] for i in range(4)]


def point_attrs(point):
    a = {}  # Attribute defaults matter: absent values are the attribute default, as in Houdini.
    for attrib in point.geometry().pointAttribs():
        if attrib.name() in ("P", "N", "v", "up", "trans", "pivot", "scale", "pscale", "orient", "rot"):
            a[attrib.name()] = point.attribValue(attrib)
    return a


references = []


def reference(name, prototype_obj, matrix, material, cd=None, pscale_field=None):
    node = root.createNode("geo", "ref_" + name)
    for child in node.children():
        child.destroy()
    merge = node.createNode("object_merge")
    merge.parm("objpath1").set(prototype_obj.renderNode().path())
    merge.parm("xformtype").set(0)
    last = merge
    if cd is not None:
        w = node.createNode("attribwrangle")
        w.setInput(0, merge)
        w.parm("class").set(0)
        w.parm("snippet").set("v@Cd={%g,%g,%g};" % tuple(cd))
        last = w
    if pscale_field is not None:
        w2 = node.createNode("attribwrangle")
        w2.setInput(0, last)
        w2.parm("class").set(0)
        w2.parm("snippet").set("f@pscale=%r;" % pscale_field)
        last = w2
    last.setRenderFlag(True)
    last.setDisplayFlag(True)
    node.setPreTransform(hou.Matrix4([x for row in matrix for x in row]))
    if material:
        node.parm("shop_materialpath").set(material.path())
    references.append(node)


expected_materials = {}
for obj_node, instancer_material, explicit in ((inst, GREEN, True), (geo_attr, GREEN, False)):
    geo = obj_node.renderNode().geometry()
    for point in geo.points():
        target = obj_node.node(point.attribValue("instance"))
        point_material = point.attribValue("shop_materialpath") if geo.findPointAttrib("shop_materialpath") else ""
        proto_material = target.parm("shop_materialpath").eval()
        material = (
            hou.node(point_material)
            if point_material
            else hou.node(proto_material)
            if proto_material
            else instancer_material
        )
        if geo.findPointAttrib("material_override") and point.attribValue("material_override"):
            material = CYAN  # Q3: a point override without a point material layers on the prototype's red.
        cd = point.attribValue("Cd") if geo.findPointAttrib("Cd") else None
        pscale_field = point.attribValue("pscale") if target == protoD else None
        world = oracle.instance_world(
            m4(target.worldTransform()), oracle.point_matrix(point_attrs(point)), m4(obj_node.worldTransform())
        )
        reference(f"{obj_node.name()}_{point.number()}", target, world, material, cd, pscale_field)
        expected_materials[f"{obj_node.name()}:{point.number()}"] = material.path()
for point in subnet_inst.renderNode().geometry().points():
    for member in assembly.children():
        world = oracle.instance_world(
            m4(member.worldTransform()), oracle.point_matrix(point_attrs(point)), m4(subnet_inst.worldTransform())
        )
        reference(
            f"subnet_{point.number()}_{member.name()}", member, world, hou.node(member.parm("shop_materialpath").eval())
        )
reference("off", protoA, oracle.mul(m4(protoA.worldTransform()), m4(off.worldTransform())), RED)

rop = hou.node("/out").createNode("cycles_ipr", "instancing_rop")
from cycles_houdini.device_selection import set_devices

set_devices(rop, args.device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot="/obj",
        camera=cam.path(),
        resx=W,
        resy=H,
        samples=SAMPLES,
        ipr_samples=SAMPLES,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        use_material_assignments=1,
        allowmotionblur=0,
        seed=3,
        animate_seed=0,
    ),
)
background = rop.parmTuple("background")
if background:
    background.set((0, 0, 0))


def render(objects, label, session):
    rop.parm("vobject").set(objects)
    update = native_scene.capture(rop)
    frame = session.wait(session.apply(update))
    status = session.status()
    p = frame["planes"]["combined"]
    image = np.frombuffer(p["pixels"], np.float32).reshape(H, W, p["channels"]).copy()
    np.save(out / (label + ".npy"), image)
    return update, image, status


with ch.Session(
    device=args.device, width=W, height=H, samples=SAMPLES, collect_diagnostics=True, use_texture_cache=False
) as session:
    update, instanced, status = render("inst inst_off scatter inst_subnet", "instanced", session)
    issues = status.get("source_diagnostics", [])
    check("instancing renders without diagnostics", not issues, diagnostics=issues)
    keys = [r["object_key"] for r in update["geometry"]]
    check(
        "scene requests: Instance OBJs instance; the s@instance object keeps an ordinary request plus instances",
        sum(1 for r in update["geometry"] if "point_instancing" in r) == 4 and len(update["geometry"]) == 5,
        keys=keys,
    )
    _, reference_image, ref_status = render(" ".join(n.name() for n in references), "reference", session)
    diff = np.abs(instanced - reference_image)
    covered = reference_image[:, :, 3] > 0
    check(
        "instanced image equals the oracle-placed reference objects",
        covered.sum() > 500 and float(diff.max()) < 0.08 and float((diff > 2e-3).mean()) < 0.01,
        max_diff=float(diff.max()),
        fraction_over=float((diff > 2e-3).mean()),
        covered=int(covered.sum()),
    )
    # Material style sheet on the instancer: same result as on a Copy-to-Points packed twin of it.
    import json as _json

    styled = wrangle_object(
        "instance",
        "inst_styled",
        'int a=addpoint(0,{-1,0.2,0}); int b=addpoint(0,{1,-0.3,0}); setpointattrib(0,"orient",b,quaternion(radians(30),{0,0,1}));',
        t=(0, 0, 0),
    )
    styled.parm("instancepath").set("../protoA")
    styled.parm("ptinstance").set("fast")
    origin = root.createNode("null", "origin_for_twin")
    twin = root.createNode("geo", "styled_twin")
    for child in twin.children():
        child.destroy()
    pts_merge = twin.createNode("object_merge")
    pts_merge.parm("objpath1").set(styled.renderNode().path())
    pts_merge.parm("xformtype").set(0)
    proto_merge = twin.createNode("object_merge")
    proto_merge.parm("objpath1").set(protoA.renderNode().path())
    proto_merge.parm("xformtype").set(2)
    proto_merge.parm("xformpath").set(origin.path())
    packed = twin.createNode("pack")
    packed.setInput(0, proto_merge)
    bind = twin.createNode("attribwrangle")
    bind.setInput(0, packed)
    bind.parm("class").set(1)
    bind.parm("snippet").set('s@shop_materialpath="%s";' % RED.path())
    copies = twin.createNode("copytopoints::2.0")
    copies.setInput(0, bind)
    copies.setInput(1, pts_merge)
    copies.setRenderFlag(True)
    copies.setDisplayFlag(True)
    rule = _json.dumps(dict(styles=[dict(target={}, overrides={"material": {"name": BLUE.path()}})]))
    for node in (styled, twin):
        node.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Style Sheet", 1))
        node.parm("shop_materialstylesheet").set(rule)
    _, styled_image, st = render("inst_styled", "styled_instances", session)
    _, twin_image, _ = render("styled_twin", "styled_twin", session)
    d = np.abs(styled_image - twin_image)
    check(
        "a material style sheet on the instancer acts like on the packed twin",
        float(d.max()) < 0.08 and float((d > 2e-3).mean()) < 0.01 and float(styled_image[..., 2].max()) > 0.5,
        max_diff=float(d.max()),
        blue=float(styled_image[..., 2].max()),
        diagnostics=st.get("source_diagnostics"),
    )
    # Dielectric priority: instancer property and per-point override (feature on).
    rop.parm("nested_dielectrics").set(1)
    from cycles_houdini import object_properties

    object_properties.install(inst)
    inst.parm("cycles_dielectric_priority").set(4)
    w = inst.renderNode()
    w.parm("snippet").set(
        w.parm("snippet").eval()
        + 'setpointattrib(0,"cycles_dielectric_priority",a,9); setpointattrib(0,"cycles_dielectric_priority",b,-3);'
    )
    update, _, status = render("inst", "priority", session)
    engine = dict(
        objects=status.get("dielectric_priority_objects"),
        low=status.get("dielectric_priority_min"),
        high=status.get("dielectric_priority_max"),
    )
    # Five instances: 9, -3 from points; the others inherit 4 ... the attribute exists on all points (0 default) -> 0.
    check(
        "per-point cycles_dielectric_priority overrides the instancer (Instance OBJ) priority",
        engine["low"] == -3 and engine["high"] == 9,
        engine=engine,
    )
    stats = {k: v for k, v in session.status().items() if k.startswith("point_instance")}
    check("point instance counters", stats.get("point_instances_synthesized", 0) > 0, stats=stats)
json.dump(dict(results=results, expected_materials=expected_materials), open(out / "render.json", "w"), indent=1)
print("RESULT", "PASS" if not failures else "FAIL " + ", ".join(failures))
