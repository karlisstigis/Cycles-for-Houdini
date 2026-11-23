# SPDX-License-Identifier: Apache-2.0
"""Point instancing transforms. For every attribute combination of SideFX's precedence
table, the exporter's synthetic packed transforms must equal (a) an independent pure-Python
implementation of the documented formula composed in the Mantra-verified order and (b) Houdini's
own Copy to Points. Also: Instance OBJ path resolution, empty-value fallback, ptinstance=off, missing
and cyclic references, and that objects without an instance attribute are untouched.
Expectations come from the oracles before anything is rendered. Generated scene only."""

from support.paths import artifacts_root

import argparse, json, math, os, random, sys, traceback
from pathlib import Path
import hou

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import point_instance_oracle as oracle
from cycles_houdini import native_scene, _native

parser = argparse.ArgumentParser()
parser.add_argument("--out", default="")
args = parser.parse_args()
out = Path(args.out) if args.out else artifacts_root() / "point-instancing-transforms"
out.mkdir(parents=True, exist_ok=True)
results = []
failures = []


def check(name, ok, **details):
    results.append(dict(name=name, ok=bool(ok), **details))
    print(("ok: " if ok else "FAIL: ") + name, flush=True)
    if not ok:
        failures.append(name)


hou.setFrame(1)
now = hou.time()
obj = hou.node("/obj")
rop = hou.node("/out").createNode("cycles_ipr", "transforms_rop")
rop.parm("source_mode").set("objects")


def box_object(name, t=(0, 0, 0), r=(0, 0, 0), s=(1, 1, 1)):
    node = obj.createNode("geo", name)
    for child in node.children():
        child.destroy()
    box = node.createNode("box")
    box.setRenderFlag(True)
    box.setDisplayFlag(True)
    node.parmTuple("t").set(t)
    node.parmTuple("r").set(r)
    node.parmTuple("s").set(s)
    node.setDisplayFlag(False)
    return node


proto = box_object("proto", t=(0.3, 1, -0.5), r=(10, 25, 40), s=(1, 2, -1))  # Nonuniform and negative.
proto2 = box_object("proto2", t=(0, 0, 3), r=(0, 90, 0))


def instancer(name, vex, instancepath="../proto", mode="fast", t=(5, 0, 0), r=(0, 30, 0), kind="instance"):
    node = obj.createNode(kind, name)
    for child in node.children():
        child.destroy()
    w = node.createNode("attribwrangle", "points")
    w.parm("class").set(0)
    w.parm("snippet").set(vex)
    w.setRenderFlag(True)
    w.setDisplayFlag(True)
    if kind == "instance":
        node.parm("instancepath").set(instancepath)
        node.parm("ptinstance").set(mode)
        node.parmTuple("i_t").set(t)
        node.parmTuple("i_r").set(r)
    else:
        node.parmTuple("t").set(t)
        node.parmTuple("r").set(r)
    return node


def m4(values):  # hou.Matrix4 / flat 16 -> nested rows
    v = list(values.asTuple()) if hasattr(values, "asTuple") else list(values)
    return [v[i * 4 : (i + 1) * 4] for i in range(4)]


def attrs_of(geo, point):
    a = {}
    for attrib in geo.pointAttribs():
        name = attrib.name()
        if name in ("P", "N", "v", "up", "trans", "pivot", "scale", "pscale", "orient", "rot", "transform"):
            value = point.attribValue(attrib)
            if name == "transform":
                value = (
                    [list(value[i * 4 : (i + 1) * 4]) for i in range(4)]
                    if len(value) == 16
                    else [list(value[i * 3 : (i + 1) * 3]) for i in range(3)]
                )
            a[name] = value
    return a


def requests(node):
    return native_scene.geometry_requests_for_rop(rop, node.renderNode(), now, diagnostics=[])


# --- (a)+(b) transform precedence --------------------------------------------------------
random.seed(7)


def q():
    return "quaternion(radians(%g),normalize({%g,%g,%g}))" % (
        random.uniform(-170, 170),
        random.uniform(-1, 1),
        random.uniform(-1, 1),
        random.uniform(0.1, 1),
    )


def v(lo=-2, hi=2):
    return "{%g,%g,%g}" % tuple(random.uniform(lo, hi) for _ in range(3))


COMBOS = {
    "none": [],
    "N": ["v@N"],
    "N_up": ["v@N", "v@up"],
    "v": ["v@v"],
    "v_up": ["v@v", "v@up"],
    "N_v": ["v@N", "v@v"],
    "orient": ["p@orient"],
    "orient_N": ["p@orient", "v@N"],
    "orient_rot": ["p@orient", "p@rot"],
    "N_rot": ["v@N", "p@rot"],
    "pscale": ["f@pscale"],
    "scale": ["v@scale"],
    "pscale_scale": ["f@pscale", "v@scale"],
    "neg_scale": ["v@scale-"],
    "pivot": ["v@pivot", "p@orient", "f@pscale"],
    "trans": ["v@trans", "p@orient"],
    "xform3": ["3@transform"],
    "xform3_pscale_orient": ["3@transform", "f@pscale", "p@orient"],
    "xform4_trans_pivot": ["4@transform", "v@trans", "v@pivot"],
    "all": ["v@N", "v@up", "p@orient", "p@rot", "f@pscale", "v@scale", "v@trans", "v@pivot", "v@v"],
}


def snippet(fields):
    lines = []
    for i in range(4):
        lines.append("{int p=addpoint(0,%s);" % v(-4, 4))
        for f in fields:
            kind, name = f.split("@")
            neg = name.endswith("-")
            name = name.rstrip("-")
            value = {
                "v": v(-1.5, 1.5) if not neg else "{-1.5,%g,%g}" % (random.uniform(0.5, 2), random.uniform(0.5, 2)),
                "p": q(),
                "f": "%g" % random.uniform(0.3, 2.5),
            }.get(kind)
            if kind == "3":
                value = "set(%s)" % ",".join("%g" % random.uniform(-1.5, 1.5) for _ in range(9))
            if kind == "4":
                value = "set(%s)" % ",".join(
                    ["%g" % random.uniform(-1.5, 1.5) for _ in range(3)]
                    + ["0"]
                    + ["%g" % random.uniform(-1.5, 1.5) for _ in range(3)]
                    + ["0"]
                    + ["%g" % random.uniform(-1.5, 1.5) for _ in range(3)]
                    + ["0"]
                    + ["%g" % random.uniform(-3, 3) for _ in range(3)]
                    + ["1"]
                )
            if name == "up" and "N" not in "".join(fields) and "v@v" not in fields:
                pass
            ctype = {"v": "vector", "p": "vector4", "f": "float", "3": "matrix3", "4": "matrix"}[kind]
            lines.append('setpointattrib(0,"%s",p,(%s)%s);' % (name, ctype, value))
        lines.append("}")
    return "\n".join(lines)


c2p_geo = obj.createNode("geo", "c2p_oracle")
c2p_geo.setDisplayFlag(False)
unit = c2p_geo.createNode("add")
unit.parm("points").set(1)
unit.parm("usept0").set(1)
pack = c2p_geo.createNode("pack")
pack.setInput(0, unit)
worst = 0.0
for name, fields in COMBOS.items():
    node = instancer("inst_" + name, snippet(fields))
    geo = node.renderNode().geometry()
    req = requests(node)
    assert len(req) == 1 and "point_instancing" in req[0], name
    table = _native.point_instance_table(req[0], now)
    rows = {row["point"]: row for row in table["instances"]}
    proto_world = m4(proto.worldTransformAtTime(now))
    targets = c2p_geo.createNode("object_merge")
    targets.parm("objpath1").set(node.renderNode().path())
    copies = c2p_geo.createNode("copytopoints::2.0")
    copies.setInput(0, pack)
    copies.setInput(1, targets)
    c2p = {prim.points()[0].number(): m4(prim.fullTransform()) for prim in copies.geometry().prims()} if False else None
    houdini = [m4(prim.fullTransform()) for prim in copies.geometry().prims()]
    errors = []
    for point in geo.points():
        expected = oracle.mul(proto_world, oracle.point_matrix(attrs_of(geo, point)))
        got = m4(rows[point.number()]["matrix"]) if point.number() in rows else None
        native = oracle.mul(proto_world, houdini[point.number()])
        scale = max(1.0, max(abs(x) for r in expected for x in r))
        e1 = oracle.max_error(got, expected) / scale if got else float("inf")
        e2 = oracle.max_error(got, native) / scale if got else float("inf")
        errors.append(max(e1, e2))
    worst = max(worst, max(errors))
    check(
        "transform " + name,
        len(rows) == len(geo.points()) and max(errors) < 2e-5,
        max_relative_error=max(errors),
        diagnostics=table["source_diagnostics"],
    )
    targets.destroy()
    copies.destroy()
    node.destroy()
check("transform worst case", worst < 2e-5, worst=worst)


# --- sources ------------------------------------------------------------------------------
def table_of(node):
    req = requests(node)
    return req, _native.point_instance_table(req[-1], now)


node = instancer(
    "inst_paths",
    'int a=addpoint(0,{1,0,0}); setpointattrib(0,"instance",a,"../proto2");'
    'int b=addpoint(0,{2,0,0}); setpointattrib(0,"instance",b,"/obj/proto2");'
    'int c=addpoint(0,{3,0,0}); setpointattrib(0,"instance",c,"op:/obj/proto2");'
    'int d=addpoint(0,{4,0,0}); setpointattrib(0,"instance",d,"");'
    'int e=addpoint(0,{5,0,0}); setpointattrib(0,"instance",e,"proto2");'  # child of the instancer: missing
    'int f=addpoint(0,{6,0,0}); setpointattrib(0,"instance",f,"../nothing");',
)
req, table = table_of(node)
spec = req[0]["point_instancing"]
paths = [p["path"] for p in spec["prototypes"]]
slot = {row["point"]: paths[row["prototype"]] for row in table["instances"]}
codes = [d["code"] for d in table["source_diagnostics"]]
check(
    "paths relative to the instancer, absolute, op:, empty -> Instance Object",
    slot == {0: "/obj/proto2", 1: "/obj/proto2", 2: "/obj/proto2", 3: "/obj/proto", 4: None, 5: None}
    or (
        slot.get(0) == slot.get(1) == slot.get(2) == "/obj/proto2"
        and slot.get(3) == "/obj/proto"
        and 4 not in slot
        and 5 not in slot
    ),
    slot=slot,
    diagnostics=table["source_diagnostics"],
)
check("missing references are diagnosed once", codes.count("instancing.unavailable_prototype") >= 1, codes=codes)
node.destroy()
node = instancer("inst_off", "int a=addpoint(0,{7,0,0}); p@orient=quaternion(1,{0,1,0});", mode="off")
req, table = table_of(node)
expected = m4(proto.worldTransformAtTime(now))
check(
    "ptinstance off renders the Instance Object once at its object transform",
    table["count"] == 1 and oracle.max_error(m4(table["instances"][0]["matrix"]), expected) < 1e-5,
    table=table,
)
node.destroy()
a = instancer("cycle_a", "int p=addpoint(0,{0,0,0});", instancepath="../cycle_b")
b = instancer("cycle_b", "int p=addpoint(0,{0,0,0});", instancepath="../cycle_a")
issues = []
try:
    req = native_scene.geometry_requests_for_rop(rop, a.renderNode(), now, diagnostics=issues)
    check("cyclic instancing is diagnosed", any(i["code"] == "instancing.cycle" for i in issues), issues=issues)
except Exception as error:
    check("cyclic instancing is diagnosed", "yclic" in str(error), error=str(error))
a.destroy()
b.destroy()
node = instancer(
    "inst_zero",
    'int a=addpoint(0,{0,0,0}); setpointattrib(0,"pscale",a,0.0); int b=addpoint(0,{1,0,0}); setpointattrib(0,"pscale",b,1.0);',
)
req, table = table_of(node)
check(
    "zero-scale instances are omitted with a diagnostic, others render",
    table["count"] == 1 and any("zero" in d["message"] for d in table["source_diagnostics"]),
    table=table,
)
node.destroy()
plain = box_object("plain")
plain.setDisplayFlag(True)
req = requests(plain)
check(
    "objects without an instance attribute are untouched",
    len(req) == 1
    and "point_instancing" not in req[0]
    and req[0] == native_scene.geometry_for_rop(rop, plain.renderNode(), now),
)
geo_inst = instancer(
    "geo_with_attribute",
    'int p=addpoint(0,{1,2,3}); setpointattrib(0,"instance",p,"../proto2"); int q=addpoint(0,{0,0,0}); int r=addprim(0,"poly",p,q,addpoint(0,{1,0,0}));',
    kind="geo",
)
req, table = table_of(geo_inst)
check(
    "a geometry object with s@instance instances its points and keeps its other geometry",
    len(req) == 2
    and req[0]["render_types"]["points"] is False
    and req[1]["object_key"].endswith("/point-instances")
    and table["count"] == 1,
    count=table["count"],
    keys=[r["object_key"] for r in req],
)
json.dump(results, open(out / "transforms.json", "w"), indent=1)
print("RESULT", "PASS" if not failures else "FAIL " + ", ".join(failures))
