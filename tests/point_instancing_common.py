# SPDX-License-Identifier: Apache-2.0
"""Point-instancing validation: shared, independent helpers.

The packed twin is built from Houdini-native SOPs only (Python SOP resolving s@instance with
hou.Node.node() relative to the instancer, Blast, Pack, Copy to Points 2.0, explicit attribute copy),
so it shares no code with the exporter's point-instance synthesis. Twin OBJ is parented to the
instancer so twin.world == instancer.world at every time; prototype OBJ world transforms are baked
into the merged prototype geometry (Object Merge into a world-origin null), giving
world = prototype.world . point . instancer.world (Mantra-verified Instance OBJ order).
Materials follow Houdini precedence independently: prims inside the prototype > point
shop_materialpath > prototype OBJ material (written onto the packed prim) > instancer OBJ material
(on the twin OBJ)."""

import json
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import materials

INSTANCE_ATTRIBS = ("Cd", "Alpha", "id", "name", "v", "w", "cycles_dielectric_priority", "mycustom")


def pick_device(want):
    want = want.lower()
    devs = ch.devices()
    if want == "cpu":
        c = [d for d in devs if d["backend"].upper() == "CPU"]
    elif want in ("3080", "4090"):
        c = [d for d in devs if d["backend"].upper() == "OPTIX" and want in d["name"]]
    else:
        c = [d for d in devs if d["id"].lower() == want]
    if len(c) != 1:
        raise RuntimeError("device %r not unique/available: %r" % (want, devs))
    return c[0]


def material(name, shader, color=None, strength=None):
    m = materials.create_material(name=name, shader=shader)
    s = m.node("surface_shader")
    if color is not None:
        s.parmTuple("color").set(color)
    if strength is not None and s.parm("strength"):
        s.parm("strength").set(strength)
    return m


def closure_output(node):
    return next(n for n, t in zip(node.outputNames(), node.outputDataTypes()) if t == "bsdf")


def attribute_material(name, color_attr="Cd", alpha_attr=None, shader="emission"):
    """Material whose colour is an attribute; optional alpha attribute mixes in transparency."""
    m = materials.create_material(name=name, shader=shader)
    s = m.node("surface_shader")
    if s.parm("strength"):
        s.parm("strength").set(1)
    a = m.createNode("cycles::attribute::1", "col")
    a.parm("attribute").set(color_attr)
    s.setNamedInput("color", a, "color")
    if alpha_attr:
        t = m.createNode("cycles::transparent_bsdf::1")
        mix = m.createNode("cycles::mix_closure::1")
        f = m.createNode("cycles::attribute::1", "alpha")
        f.parm("attribute").set(alpha_attr)
        mix.setNamedInput("fac", f, "fac" if "fac" in f.outputNames() else f.outputNames()[0])
        mix.setNamedInput("closure1", t, closure_output(t))
        mix.setNamedInput("closure2", s, closure_output(s))
        m.node("surface_output").setNamedInput("surface", mix, closure_output(mix))
    return m


def geo_obj(name, sop_type=None, parent=None, **parms):
    root = parent or hou.node("/obj")
    node = root.createNode("geo", name)
    for c in node.children():
        c.destroy()
    shape = None
    if sop_type:
        shape = node.createNode(sop_type)
        for k, v in parms.items():
            (shape.parmTuple(k).set(v) if isinstance(v, tuple) else shape.parm(k).set(v))
        shape.setRenderFlag(True)
        shape.setDisplayFlag(True)
    return node, shape


def wrangle_points(obj, snippet, name="points", extra_int_parms=()):
    w = obj.createNode("attribwrangle", name)
    w.parm("class").set(0)
    for p, default in extra_int_parms:
        w.addSpareParmTuple(hou.IntParmTemplate(p, p, 1, default_value=(default,)))
    w.parm("snippet").set(snippet)
    w.setRenderFlag(True)
    w.setDisplayFlag(True)
    return w


def origin_null():
    n = hou.node("/obj/E_world_origin")
    return n or hou.node("/obj").createNode("null", "E_world_origin")


RESOLVE = """
node = hou.pwd(); geo = node.geometry()
inst = hou.node(node.evalParm('instancer')); keys = node.evalParm('prototypes').split()
fallback = inst.parm('instancepath').eval() if inst.parm('instancepath') else ''
slot = {}
a = geo.findPointAttrib('instance')
values = list(a.strings()) if a else []
for s in values + ['']:
    target = inst.node(s or fallback) if (s or fallback) else None
    slot[s] = keys.index(target.path()) if target is not None and target.path() in keys else -1
geo.addAttrib(hou.attribType.Point, '__proto', -1); geo.addAttrib(hou.attribType.Point, '__srcpt', -1)
strings = geo.pointStringAttribValues('instance') if a else [''] * len(geo.iterPoints())
geo.setPointIntAttribValues('__proto', [slot.get(s, -1) for s in strings])
geo.setPointIntAttribValues('__srcpt', list(range(len(strings))))
"""


TWIN_VEX = """int pt = primpoint(0, @primnum, 0); int src = @primnum;  // one packed prim per target point, in order
if (haspointattrib(1, "Cd")) { vector x = point(1, "Cd", src); setpointattrib(0, "Cd", pt, x); }
if (haspointattrib(1, "v")) { vector x = point(1, "v", src); setpointattrib(0, "v", pt, x); }
if (haspointattrib(1, "w")) { vector x = point(1, "w", src); setpointattrib(0, "w", pt, x); }
if (haspointattrib(1, "Alpha")) { float x = point(1, "Alpha", src); setpointattrib(0, "Alpha", pt, x); }
if (haspointattrib(1, "mycustom")) { float x = point(1, "mycustom", src); setpointattrib(0, "mycustom", pt, x); }
if (haspointattrib(1, "id")) { int x = point(1, "id", src); setpointattrib(0, "id", pt, x); }
if (haspointattrib(1, "cycles_dielectric_priority")) { int x = point(1, "cycles_dielectric_priority", src); setpointattrib(0, "cycles_dielectric_priority", pt, x); }
if (haspointattrib(1, "name")) { string x = point(1, "name", src); setpointattrib(0, "name", pt, x); }
i@__srcpt = point(1, "__srcpt", src); i@__proto = %d;
string m = ""; if (haspointattrib(1, "shop_materialpath")) m = point(1, "shop_materialpath", src);
if (m == "") m = "%s";
if (m != "") s@shop_materialpath = m;
if (haspointattrib(1, "material_override")) { string x = point(1, "material_override", src); s@material_override = x; }
"""


def build_twin(name, instancer, prototypes, instancer_material=None, nested_sources=None, render_node=None):
    """Packed twin of an instancer. prototypes: list of prototype OBJ nodes (slot order).
    nested_sources: {prototype path: SOP node path giving that prototype's geometry in WORLD space}
    (used for nested instancers, whose own twin supplies the geometry)."""
    nested_sources = nested_sources or {}
    origin = origin_null()
    twin = hou.node("/obj").createNode("geo", name)
    for c in twin.children():
        c.destroy()
    twin.setFirstInput(instancer)  # twin.world = instancer.world at every time
    pts = twin.createNode("object_merge", "points")
    pts.parm("objpath1").set((render_node or instancer.renderNode()).path())
    pts.parm("xformtype").set(0)
    res = twin.createNode("python", "resolve")
    res.setInput(0, pts)
    ptg = res.parmTemplateGroup()
    ptg.append(hou.StringParmTemplate("instancer", "instancer", 1, default_value=(instancer.path(),)))
    ptg.append(
        hou.StringParmTemplate("prototypes", "prototypes", 1, default_value=(" ".join(p.path() for p in prototypes),))
    )
    res.setParmTemplateGroup(ptg)
    res.parm("python").set(RESOLVE)
    merge = twin.createNode("merge", "all")
    for k, proto in enumerate(prototypes):
        om = twin.createNode("object_merge", "proto%d" % k)
        if proto.path() in nested_sources:
            om.parm("objpath1").set(nested_sources[proto.path()])
        else:
            om.parm("objpath1").set(proto.renderNode().path())
        om.parm("xformtype").set(2)
        om.parm("xformpath").set(origin.path())
        pack = twin.createNode("pack", "pack%d" % k)
        pack.setInput(0, om)
        if pack.parm("pivot"):
            pack.parm("pivot").set(0)
        sel = twin.createNode("blast", "select%d" % k)
        sel.setInput(0, res)
        sel.parm("grouptype").set("points")
        sel.parm("group").set("@__proto==%d" % k)
        sel.parm("negate").set(1)  # keep the selected points
        ctp = twin.createNode("copytopoints::2.0", "copy%d" % k)
        ctp.setInput(0, pack)
        ctp.setInput(1, sel)
        proto_mat = proto.parm("shop_materialpath").eval() if proto.parm("shop_materialpath") else ""
        w = twin.createNode("attribwrangle", "attrs%d" % k)
        w.setInput(0, ctp)
        w.setInput(1, sel)
        w.parm("class").set(1)  # prims
        w.parm("snippet").set(TWIN_VEX % (k, proto_mat))
        clean = twin.createNode("attribdelete", "clean%d" % k)
        clean.setInput(0, w)
        clean.parm("ptdel").set("instance shop_materialpath material_override __proto")
        merge.setNextInput(clean)
    merge.setRenderFlag(True)
    merge.setDisplayFlag(True)
    if instancer_material is not None:
        twin.parm("shop_materialpath").set(
            instancer_material.path() if hasattr(instancer_material, "path") else instancer_material
        )
    # NOTE: the twin must stay displayed: the ROP's object list skips undisplayed objects.
    return twin


def m4(values):
    v = list(values.asTuple()) if hasattr(values, "asTuple") else list(values)
    return np.array(v, dtype=np.float64).reshape(4, 4)


def twin_native_transforms(twin, t=None):
    """Houdini's own evaluation: packedfulltransform of each twin packed prim (instancer-local space,
    the prototype OBJ world baked into the geometry)."""
    geo = twin.renderNode().geometryAtFrame(hou.timeToFrame(t)) if t is not None else twin.renderNode().geometry()
    rows = {}
    for prim in geo.prims():
        if not isinstance(prim, hou.PackedPrim):
            continue
        src = prim.attribValue("__srcpt")
        slot = prim.attribValue("__proto")
        rows[src] = (slot, m4(prim.intrinsicValue("packedfulltransform")))
    return rows


def compare_table_to_twin(table, request, twin, t=None):
    """table: _native.point_instance_table(request, t) (instancer OBJ excluded, prototype OBJ world included).
    Houdini-native expectation: prototype.world(t) . twin packed full transform."""
    protos = [p["path"] for p in request["point_instancing"]["prototypes"]]
    native = twin_native_transforms(twin, t)
    worst, missing, slot_mismatch = 0.0, [], []
    seen = set()
    for row in table["instances"]:
        src = row["point"]
        seen.add(src)
        if src not in native:
            missing.append(src)
            continue
        tslot, full = native[src]
        proto_node = hou.node(protos[row["prototype"]]) if 0 <= row["prototype"] < len(protos) else None
        if proto_node is None:
            slot_mismatch.append((src, row["prototype"]))
            continue
        pw = m4(proto_node.worldTransformAtTime(t if t is not None else hou.time()))
        expected = pw @ full
        got = m4(row["matrix"])
        scale = max(1.0, float(np.abs(expected).max()))
        worst = max(worst, float(np.abs(got - expected).max()) / scale)
    extra = sorted(set(native) - seen)
    return dict(
        worst_rel=worst,
        rows=len(table["instances"]),
        twin_rows=len(native),
        missing_in_twin=missing,
        missing_in_table=extra,
        slot_mismatch=slot_mismatch,
    )


# --- images and Cryptomatte (independent MurmurHash3, as the existing cryptomatte smoke) -------------
def plane(frame, name):
    v = frame["planes"][name]
    return np.frombuffer(v["pixels"], np.float32).reshape(frame["height"], frame["width"], v["channels"]).copy()


def murmur32(name):
    data = name.encode("utf-8")
    h = 0
    rot = lambda value, n: ((value << n) | (value >> (32 - n))) & 0xFFFFFFFF

    def block(k):
        return (rot(k * 0xCC9E2D51 & 0xFFFFFFFF, 15) * 0x1B873593) & 0xFFFFFFFF

    full = len(data) // 4 * 4
    for o in range(0, full, 4):
        h ^= block(int.from_bytes(data[o : o + 4], "little"))
        h = (rot(h, 13) * 5 + 0xE6546B64) & 0xFFFFFFFF
    if full != len(data):
        h ^= block(int.from_bytes(data[full:], "little"))
    h ^= len(data)
    h ^= h >> 16
    h = h * 0x85EBCA6B & 0xFFFFFFFF
    h ^= h >> 13
    h = h * 0xC2B2AE35 & 0xFFFFFFFF
    h ^= h >> 16
    return h


def id_bits(name):
    h = murmur32(name)
    if h >> 23 & 255 in (0, 255):
        h ^= 1 << 23
    return h


def matte(frame, family, name):
    r = np.zeros((frame["height"], frame["width"]), np.float32)
    for p in sorted(p for p in frame["planes"] if p.startswith(family)):
        px = plane(frame, p)
        for o in (0, 2):
            r += (px[:, :, o].copy().view(np.uint32) == id_bits(name)) * px[:, :, o + 1]
    return r


def manifests(metadata):
    result = {}
    for key, layer in metadata.items():
        if key.startswith("cryptomatte/") and key.endswith("/name"):
            result[layer] = json.loads(metadata[key[:-4] + "manifest"])
    return result


def image_compare(a, b, alpha_index=3):
    d = np.abs(a - b)
    covered = (
        (a[:, :, alpha_index] > 0) | (b[:, :, alpha_index] > 0)
        if a.shape[2] > alpha_index
        else np.ones(a.shape[:2], bool)
    )
    return dict(
        max_abs=float(d.max()),
        mean_abs=float(d.mean()),
        mean_abs_covered=float(d[covered].mean()) if covered.any() else 0.0,
        frac_over_0p02=float((d.max(axis=2) > 0.02).mean()),
        covered=int(covered.sum()),
        identical=bool(np.array_equal(a, b)),
    )
