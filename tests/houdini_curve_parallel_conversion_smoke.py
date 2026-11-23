# SPDX-License-Identifier: Apache-2.0
"""Parallel curve conversion equals the serial schedule, value for value.

The production curve converter (capture_curves) runs once with
CYCLES_HOUDINI_SERIAL_CONVERSION=1 and once with the parallel schedule on the
same frozen details: ribbons with widths and curves of 1 to 15 points
(1-point curves are omitted with a diagnostic), named point/vertex/primitive
fields with Cd and uv, non-finite positions/widths/fields (every diagnostic must
name the same first curve and issue), Bezier/NURBS curves sampled per span and
mixed with polygon curves, curves mixed with closed polygons in one SOP, and
several materials bound per primitive and per point. Every output array, the
material palette, the identity fallback count and the diagnostics must be equal.
The parallel path takes open polygon curves only: Bezier/NURBS input must
report the serial converter, polygon curves the parallel one. Then the whole
pipeline: the curve shapes (ribbon, 3D, linear) with blur off, transform,
velocity, acceleration and deformation blur, two frames each, rendered under
both schedules: payloads, images and a hash of what the worker wrote into the
engine's hair arrays (CYCLES_HOUDINI_HASH_CURVES=1, core/session_strands.cpp)
must be equal. The report also lists a digest of each case, so two builds can
be compared.
"""

from support.paths import artifacts_root
import argparse
import hashlib
import json
import os
import traceback
import time
import glob
import tempfile
import hou
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene
from cycles_houdini.ipr import queued, registry
from cycles_houdini.scene import object_properties

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")  # Render device of the pipeline part (exact id for GPUs).
device = parser.parse_args().device
dest = artifacts_root() / "stage2/hair/parallel-conversion"
# Worker logs carry the post-apply hair hash lines.
LOGDIR = tempfile.mkdtemp(prefix="curve_parallel_")
os.environ["CYCLES_HOUDINI_WORKER_LOG_DIR"] = LOGDIR
os.environ["CYCLES_HOUDINI_HASH_CURVES"] = "1"
SEEN = {}


def hair_hashes():
    out = []
    for path in sorted(glob.glob(os.path.join(LOGDIR, "*"))):
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        for line in lines[SEEN.get(path, 0) :]:
            if line.startswith("CH_CURVE_HASH "):
                record = json.loads(line[len("CH_CURVE_HASH ") :])
                record.pop("used_shaders", None)  # Material keys are per process.
                out.append(record)
        SEEN[path] = len(lines)
    return out


dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, cases={})

COMMON = r"""
import math
node = hou.pwd()
geo = node.geometry()
geo.clear()
case = node.evalParm("case")
def ramp(i, k, n):
    return k / float(max(n - 1, 1))
def lines(count, sizes, start=0.0):
    positions, prims = [], []
    for c in range(count):
        n = sizes(c)
        x, z = (c % 150) * 0.07, (c // 150) * 0.07 + start
        first = len(positions)
        for k in range(n):
            u = ramp(c, k, n)
            positions.append(hou.Vector3(x + 0.01 * math.sin(c + k), 0.4 * u, z + 0.01 * math.cos(1.7 * c + k)))
        prims.append(tuple(range(first, first + n)))
    return positions, prims
def build(pts, prims):
    # createPolygons() rejects 1-point open polygons; keep primitive order.
    result, batch = [], []
    def flush():
        if batch:
            result.extend(geo.createPolygons(tuple(tuple(pts[i] for i in p) for p in batch), False))
            batch.clear()
    for p in prims:
        if len(p) >= 2:
            batch.append(p)
            continue
        flush()
        poly = geo.createPolygon(False)
        for i in p:
            poly.addVertex(pts[i])
        result.append(poly)
    flush()
    return result
"""

CASES = {
    "ribbons": r"""
positions, prims = lines(20000, lambda c: 1 + (c * 7) % 15)
pts = geo.createPoints(positions)
build(pts, prims)
geo.addAttrib(hou.attribType.Point, "width", 0.02)
geo.addAttrib(hou.attribType.Point, "v", (0.0, 0.0, 0.0))
geo.setPointFloatAttribValues("width", [0.01 + 0.02 * ((i * 13) % 7) / 7.0 for i in range(len(pts))])
geo.setPointFloatAttribValues("v", [x for i in range(len(pts)) for x in (0.1 * math.sin(i), 0.2, 0.05 * math.cos(i))])
""",
    "fields": r"""
positions, prims = lines(15000, lambda c: 2 + (c * 5) % 9)
pts = geo.createPoints(positions)
polys = build(pts, prims)
n = len(pts)
geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
geo.setPointFloatAttribValues("Cd", [x for i in range(n) for x in (((i * 3) % 10) / 10.0, 0.5, ((i * 7) % 10) / 10.0)])
geo.addAttrib(hou.attribType.Point, "signal", 0.0)
geo.setPointFloatAttribValues("signal", [math.sin(0.01 * i) for i in range(n)])
geo.addAttrib(hou.attribType.Vertex, "custom", (0.0, 0.0, 0.0))
geo.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
geo.addAttrib(hou.attribType.Prim, "prim_field", 0.0)
geo.addAttrib(hou.attribType.Prim, "width", 0.02)
geo.addAttrib(hou.attribType.Prim, "id", 0)
geo.addAttrib(hou.attribType.Prim, "name", "")
for c, prim in enumerate(polys):
    prim.setAttribValue("prim_field", 0.001 * c)
    prim.setAttribValue("width", 0.01 + 0.001 * (c % 9))
    prim.setAttribValue("id", c * 3 if c % 50 else 7)  # duplicate ids fall back to the index
    prim.setAttribValue("name", "strand%d" % c if c % 3 == 0 else "")
    for k, vertex in enumerate(prim.vertices()):
        vertex.setAttribValue("custom", (c * 0.001, k * 0.1, 0.5))
        vertex.setAttribValue("uv", (c * 0.0001, k * 0.01, 0.0))
""",
    "invalid": r"""
positions, prims = lines(12000, lambda c: 1 if c == 2 else 2 + (c * 3) % 8)
pts = geo.createPoints(positions)
polys = build(pts, prims)
nan, inf = float("nan"), float("inf")
# Failures in the last and the first chunks: the reported first failure must not
# depend on which chunk finishes first.
for c in (11995, 5000, 37, 1200):
    pts[prims[c][1]].setPosition((nan, 0.0, 0.0))
pts[prims[2400][0]].setPosition((0.0, inf, 0.0))
geo.addAttrib(hou.attribType.Point, "width", 0.02)
geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
geo.addAttrib(hou.attribType.Point, "signal", 0.0)
geo.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
pts[prims[900][1]].setAttribValue("width", -1.0)
pts[prims[300][0]].setAttribValue("width", nan)
pts[prims[3000][1]].setAttribValue("Cd", (nan, 0.0, 0.0))
pts[prims[2000][0]].setAttribValue("Cd", (0.0, inf, 0.0))
pts[prims[4000][1]].setAttribValue("signal", nan)
pts[prims[3500][0]].setAttribValue("signal", inf)
for c in (11990, 3):
    pts[prims[c][1]].setAttribValue("width", -2.0)
    pts[prims[c][1]].setAttribValue("Cd", (0.0, 0.0, nan))
    pts[prims[c][1]].setAttribValue("signal", nan)
polys[11999].vertices()[0].setAttribValue("uv", (0.0, inf, 0.0))
polys[6000].vertices()[0].setAttribValue("uv", (nan, 0.0, 0.0))
""",
    "parametric": r"""
positions, prims = lines(6000, lambda c: 3 + c % 4)
pts = geo.createPoints(positions)
geo.addAttrib(hou.attribType.Point, "width", 0.02)
geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
geo.addAttrib(hou.attribType.Point, "signal", 0.0)
geo.addAttrib(hou.attribType.Point, "v", (0.0, 0.0, 0.0))
for c, prim in enumerate(prims):
    if c % 5 == 1:
        curve = geo.createNURBSCurve(len(prim) + 1, False, 3 if c % 2 else 4)
    elif c % 5 == 3 and len(prim) >= 4:
        curve = geo.createBezierCurve(4, False, 4)
    else:
        curve = geo.createPolygon(False)
        for i in prim:
            curve.addVertex(pts[i])
        continue
    for k, vertex in enumerate(curve.vertices()):
        p = vertex.point()
        p.setPosition((c * 0.03, 0.1 * k, 0.02 * math.sin(c + k)))
for i, p in enumerate(geo.points()):
    p.setAttribValue("width", 0.01 + 0.002 * (i % 5))
    p.setAttribValue("Cd", ((i % 10) / 10.0, 0.2, 0.7))
    p.setAttribValue("signal", math.cos(0.02 * i))
    p.setAttribValue("v", (0.0, 0.1 * math.sin(i), 0.0))
""",
    # More curves than one conversion chunk and fewer than the host's threads.
    "small": r"""
positions, prims = lines(24, lambda c: 150 + 10 * c)
pts = geo.createPoints(positions)
build(pts, prims)
geo.addAttrib(hou.attribType.Point, "width", 0.02)
geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
geo.addAttrib(hou.attribType.Point, "signal", 0.0)
geo.setPointFloatAttribValues("signal", [0.001 * i for i in range(len(pts))])
for c in (22, 1):
    pts[prims[c][5]].setAttribValue("Cd", (float("nan"), 0.0, 0.0))
    pts[prims[c][7]].setAttribValue("width", -1.0)
pts[prims[23][3]].setPosition((float("nan"), 0.0, 0.0))
""",
    # A detail Cd (one value for every key) and a per-point width.
    "detail_cd": r"""
positions, prims = lines(10000, lambda c: 2 + c % 11)
pts = geo.createPoints(positions)
build(pts, prims)
geo.addAttrib(hou.attribType.Global, "Cd", (0.2, 0.4, 0.6))
geo.addAttrib(hou.attribType.Point, "width", 0.02)
geo.setPointFloatAttribValues("width", [0.005 + 0.001 * (i % 13) for i in range(len(pts))])
""",
    "mixed": r"""
positions, prims = lines(9000, lambda c: 2 + c % 6)
pts = geo.createPoints(positions)
geo.addAttrib(hou.attribType.Point, "width", 0.015)
geo.addAttrib(hou.attribType.Point, "Cd", (0.5, 0.5, 0.5))
for c, prim in enumerate(prims):
    closed = c % 4 == 2 and len(prim) >= 3
    poly = geo.createPolygon(closed)
    for i in prim:
        poly.addVertex(pts[i])
for i, p in enumerate(geo.points()):
    p.setAttribValue("Cd", ((i % 7) / 7.0, 0.3, 0.1))
""",
}
MATERIALS = r"""
positions, prims = lines(16000, lambda c: 2 + c % 7)
pts = geo.createPoints(positions)
polys = build(pts, prims)
paths = json.loads(node.evalParm("bindings"))
geo.addAttrib(hou.attribType.Prim, "shop_materialpath", "")
for c, prim in enumerate(polys):
    prim.setAttribValue("shop_materialpath", paths[(c // 3) % len(paths)])
if case == "point_materials":
    geo.addAttrib(hou.attribType.Point, "shop_materialpath", "")
    for c in range(0, len(prims), 11):
        for i in prims[c]:
            pts[i].setAttribValue("shop_materialpath", paths[1])
"""
NAMES = ["signal", "custom", "prim_field"]


def make(obj, case, code, extra=None):
    node = obj.createNode("python", case)
    group = node.parmTemplateGroup()
    group.append(hou.StringParmTemplate("case", "Case", 1, default_value=(case,)))
    group.append(hou.StringParmTemplate("bindings", "Bindings", 1, default_value=(extra or "[]",)))
    node.setParmTemplateGroup(group)
    node.parm("python").set("import json\n" + COMMON + code)
    return node


def digest(raw):
    h = hashlib.sha256()
    for key in sorted(raw):
        value = raw[key]
        h.update(key.encode())
        if isinstance(value, bytes):
            h.update(value)
        elif key == "attributes":
            for name in sorted(value):
                h.update(name.encode() + bytes([value[name]["components"]]) + value[name]["values"])
        else:
            h.update(json.dumps(value, sort_keys=True, default=str).encode())
    return h.hexdigest()


VELOCITY = {"ribbons", "parametric"}


def convert(node, divisions):
    try:
        node.cook(force=False)
    except hou.OperationFailed:
        raise RuntimeError("%s: %s" % (node.path(), node.errors()))
    lease = ch._native.preserve_geometry(node.path(), hou.time(), True)
    try:
        velocity = node.name() in VELOCITY
        return ch._native.convert_preserved_curves(lease, divisions, velocity, NAMES)
    finally:
        del lease


try:
    obj = hou.node("/obj").createNode("geo", "parallel_curves", run_init_scripts=False)
    builders = [ch.create_material(name="parallel_strand_%d" % i, shader="emission") for i in range(3)]
    bindings = json.dumps([b.path() for b in builders] + [""])
    nodes = {case: make(obj, case, code) for case, code in CASES.items()}
    for case, node in nodes.items():
        for divisions in (1, 4) if case == "parametric" else (4,):
            results = {}
            for schedule in ("serial", "parallel"):
                if schedule == "serial":
                    os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
                else:
                    os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
                results[schedule] = convert(node, divisions)
            os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
            serial, parallel = results["serial"], results["parallel"]
            differing = sorted(k for k in set(serial) | set(parallel) if serial.get(k) != parallel.get(k))
            label = "%s/%d" % (case, divisions)
            report["cases"][label] = dict(
                digest=digest(serial),
                curves=len(serial["first_key"]) // 4,
                keys=len(serial["radii"]) // 4,
                materials=serial["materials"],
                index_ids=serial["index_ids"],
                attributes=sorted(serial["attributes"]),
                diagnostics=serial["source_diagnostics"],
                differing=differing,
            )
            assert not differing, (label, differing)
            assert serial["first_key"], label
            # Bezier/NURBS take the serial converter; open polygon curves the parallel one.
            assert serial["parallel"] == (case != "parametric"), (label, serial["parallel"])
    # Native material bindings, identities and motion go through the whole
    # pipeline: the first encoded scene of a fresh session under each schedule.
    for case in ("materials", "point_materials"):
        nodes[case] = make(obj, case, MATERIALS, bindings)
    payloads = []

    class Proxy:
        def __init__(self, inner):
            object.__setattr__(self, "_inner", inner)

        def __getattr__(self, name):
            value = getattr(object.__getattribute__(self, "_inner"), name)
            if name != "encode_snapshot":
                return value

            def wrapped(desired, *args, **kwargs):
                payload = value(desired, *args, **kwargs)
                payloads.append(hashlib.sha256(bytes(payload)).hexdigest())
                return payload

            return wrapped

    queued._native = Proxy(queued._native)
    camera = hou.node("/obj").createNode("cam", "parallel_curves_camera")
    camera.setParms(dict(tx=5, ty=0.2, tz=12, resx=64, resy=48))
    object_properties.install(obj)
    obj.parm("shop_materialpath").set(builders[0].path())
    rop = hou.node("/out").createNode("cycles_ipr", "parallel_curves_cycles")
    device_selection.set_devices(rop, [device] if device != "CPU" else "CPU")
    rop.setParms(
        dict(
            source_mode="sop",
            camera=camera.path(),
            resx=64,
            resy=48,
            ipr_samples=1,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            use_material_assignments=1,
        )
    )
    rop.parm("ipr_samples").set(2)
    motion = rop.parm("geometry_motion")
    items = list(motion.parmTemplate().menuItems())

    def item(*words):
        for word in words:
            for token in items:
                if word in token.lower():
                    return token
        raise AssertionError((words, items))

    blur = {
        "transform": item("transform"),
        "velocity": item("velocity"),
        "acceleration": item("accel"),
        "samples": item("deform", "sampl"),
    }
    # A deforming wrangle (v and accel beside P) for the blur modes, two frames each.
    deform = obj.createNode("attribwrangle", "deforming")
    deform.setInput(0, nodes["ribbons"])
    deform.parm("snippet").set(
        "float u = (@ptnum % 7) / 6.0; float t = @Time * 4.0 + @ptnum * 0.01;"
        "@P += set(sin(t), 0, cos(1.3 * t)) * 0.02 * u;"
        "v@v = set(cos(t), 0, -1.3 * sin(1.3 * t)) * 0.08 * u;"
        "v@accel = set(-sin(t), 0.1, -1.69 * cos(1.3 * t)) * 0.32 * u;"
    )
    deform.cook(force=True)
    assert not deform.errors(), deform.errors()
    nodes["deforming"] = deform
    runs = [(case, None, None) for case in ("materials", "point_materials", "ribbons", "fields")]
    for shape in ("ribbon", "thick", "linear"):
        runs += [("deforming", mode, shape) for mode in (None, "transform", "velocity", "acceleration", "samples")]
    runs += [("fields", "samples", None)]
    for case, mode, shape in runs:
        rop.parm("soppath").set(nodes[case].path())
        rop.parm("allowmotionblur").set(1 if mode else 0)
        if mode:
            motion.set(blur[mode])
        if obj.parm("cycles_curve_shape"):
            obj.parm("cycles_curve_shape").set(shape or "ribbon")
        seen = {}
        for schedule in ("serial", "parallel"):
            if schedule == "serial":
                os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
            else:
                os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
            registry.close_all()
            hair_hashes()
            frames = []
            entry = None
            for frame in (1, 2) if case == "deforming" else (1,):
                hou.setFrame(frame)
                del payloads[:]
                capture = native_scene.capture(rop)
                if entry is None:
                    entry = registry.for_rop(rop, native_scene.configuration(rop))
                image = entry.session.wait(entry.apply(capture, hou.time()))
                status = entry.session.status()
                pixels = image["planes"]["combined"]["pixels"] if image else b""
                frames.append(
                    dict(
                        payloads=list(payloads),
                        image=hashlib.sha256(bytes(pixels)).hexdigest(),
                        state=status.get("state"),
                        error=status.get("error"),
                        curves=status.get("curves"),
                        motion=status.get("motion_samples"),
                        warnings=status.get("motion_warnings"),
                        diagnostics=status.get("source_diagnostics"),
                    )
                )
            registry.close_all()
            time.sleep(0.3)
            seen[schedule] = dict(frames=frames, hair=hair_hashes())
        os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
        hou.setFrame(1)
        label = "pipeline/%s/%s/%s" % (case, mode or "still", shape or "-")
        first = seen["serial"]["frames"][0]
        report["cases"][label] = dict(
            digest=(first["payloads"] or [""])[0],
            curves=first["curves"],
            keys=None,
            index_ids=None,
            diagnostics=first["diagnostics"] or [],
            state=first["state"],
            error=first["error"],
            hair=[h["hash"] for h in seen["serial"]["hair"]],
            differing=seen["serial"] != seen["parallel"],
        )
        assert seen["serial"] == seen["parallel"], (label, seen)
        assert first["payloads"] and not first["error"], (label, first)
        assert seen["serial"]["hair"], (label, "no post-apply hair hash")
    cases = report["cases"]
    ribbons = {d["code"]: d["message"] for d in cases["ribbons/4"]["diagnostics"]}
    assert "primitive indices: 0, 15, 30," in ribbons["geometry.curve_unavailable"], ribbons  # 1-point curves
    invalid = {d["code"]: d["message"] for d in cases["invalid/4"]["diagnostics"]}
    assert (
        "primitive indices: 2, 37, 1200, 2400, 5000, 11995; first issue: An open curve requires at least two"
        in (invalid["geometry.curve_unavailable"])
    ), invalid
    for code in (
        "geometry.invalid_width",
        "geometry.invalid_color",
        "geometry.invalid_uv",
        "geometry.invalid_attribute",
    ):
        assert code in invalid, (code, invalid)
    assert cases["fields/4"]["attributes"] == sorted(NAMES), cases["fields/4"]["attributes"]
    report["result"] = "passed"
except Exception:
    report["result"] = "failed"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
finally:
    os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)
    for label, value in report["cases"].items():
        print(
            "C4CASE",
            label,
            value["digest"][:16],
            value["curves"],
            value["keys"],
            value["index_ids"],
            len(value["diagnostics"]),
            flush=True,
        )
hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True)
