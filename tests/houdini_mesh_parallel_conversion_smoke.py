# SPDX-License-Identifier: Apache-2.0
"""Parallel mesh conversion equals the serial schedule, step for step.

Plain polygon meshes go through frame changes that move every point: an
all-quad mesh rebuilt by a Subdivide SOP on every cook (new topology data ids,
so the partition and the topology are compared again) with a concave and a
zero-area quad, a triangle mesh, a mixed n-gon mesh, a polygon soup, a mesh with
an open polygon, a mesh with an unreferenced point, a mesh with a packed
primitive, and a mesh with vertex uv, vertex normals and a detail Cd. Four more
meshes carry invalid values (non-finite uv, a zero-length and a non-finite normal
in either order, non-finite point Cd) at two places far apart: the one first in
triangle order chooses the diagnostic on any schedule, and the stream is dropped. Every step
is rendered once with CYCLES_HOUDINI_SERIAL_CONVERSION=1 and once with the
parallel schedule; images, the complete worker payload of each scene and the
update counters and diagnostics must be identical. With --record <file> the
serial run's digests are also written there, for comparing two builds: counters
and diagnostics compare directly; payload digests only for a scene of the first
nine meshes, because material keys differ between processes and a second keyed
material (the diagnostic variant the invalid Cd creates) lets them order the payload.
"""

from support.paths import artifacts_root
import argparse
import hashlib
import json
import os
import re
import traceback
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, native_scene
from cycles_houdini.ipr import queued

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--record", default="")
arguments = parser.parse_args()
device = arguments.device
dest = artifacts_root() / "stage3/mesh-parallel-conversion"
dest.mkdir(parents=True, exist_ok=True)
width, height = 160, 120
report = dict(result="running", device=device, steps={})

scene = hou.node("/obj").createNode("subnet", "mesh_parallel_conversion")
WAVE = "@P.y += 0.15 * sin(@P.x * 1.7 + @Frame) * cos(@P.z * 1.3 - 0.7 * @Frame);"


def pick(parm, text):
    """Menu index whose token or label contains text (case-insensitive)."""
    template = parm.parmTemplate()
    for index, (token, label) in enumerate(zip(template.menuItems(), template.menuLabels())):
        if text in token.lower() or text in label.lower():
            return index
    raise AssertionError((parm.path(), text, template.menuItems(), template.menuLabels()))


def mesh_object(name, x, z, rows=150, surface=None):
    obj = scene.createNode("geo", name, run_init_scripts=False)
    obj.parmTuple("t").set((x, 0, z))
    grid = obj.createNode("grid", "grid")
    grid.parmTuple("size").set((3, 3))
    grid.parm("rows").set(rows)
    grid.parm("cols").set(rows)
    if surface:
        grid.parm("surftype").set(pick(grid.parm("surftype"), surface))
    wave = obj.createNode("attribwrangle", "wave")
    wave.setInput(0, grid)
    wave.parm("class").set(2)  # points
    wave.parm("snippet").set(WAVE)
    return obj, wave


def output(obj, node):
    node.setRenderFlag(True)
    node.setDisplayFlag(True)


# All quads, rebuilt by the Subdivide SOP on every cook.
quads, wave = mesh_object("quads_subdivided", -6, 3, rows=76)
subdivide = quads.createNode("subdivide", "subdivide")
subdivide.setInput(0, wave)
subdivide.parm("algorithm").set("osdcc")
subdivide.parm("iterations").set(1)
shapes = quads.createNode("attribwrangle", "concave_and_zero_area")
shapes.setInput(0, subdivide)
shapes.parm("class").set(0)  # detail
shapes.parm("snippet").set(
    """
int a[] = primpoints(0, 101);
vector p0 = point(0, "P", a[0]), p1 = point(0, "P", a[1]), p2 = point(0, "P", a[2]);
vector c = 0.5 * (p0 + p2);
// Concave: corner 1 crosses the 0-2 diagonal (more on odd frames).
setpointattrib(0, "P", a[1], c + (c - p1) * (int(@Frame) % 2 ? 0.6 : 0.2));
int b[] = primpoints(0, 2203);
vector q = point(0, "P", b[0]);
if (int(@Frame) % 3 != 1) foreach (int p; b) setpointattrib(0, "P", p, q);  // zero area
int l[] = primpoints(0, 4407);
vector s = point(0, "P", l[0]), e = point(0, "P", l[2]);
for (int i = 0; i < len(l); ++i) setpointattrib(0, "P", l[i], s + (e - s) * (i / 3.0));  // collinear
"""
)
output(quads, shapes)

triangles, wave = mesh_object("triangles", -2, 3, rows=160, surface="tri")
output(triangles, wave)

ngons, wave = mesh_object("ngons", 2, 3, rows=110)
dual = ngons.createNode("divide", "dual")
dual.setInput(0, wave)
dual.parm("dual").set(1)
output(ngons, dual)

soup, wave = mesh_object("polysoup", 6, 3)
polysoup = soup.createNode("polysoup", "polysoup")
polysoup.setInput(0, wave)
output(soup, polysoup)


def merged(name, x, z, extra):
    obj, wave = mesh_object(name, x, z)
    merge = obj.createNode("merge", "merge")
    merge.setInput(0, wave)
    merge.setInput(1, extra(obj))
    output(obj, merge)


def open_polygon(obj):
    line = obj.createNode("line", "open_polygon")
    line.parm("points").set(5)
    return line


def loose_point(obj):
    add = obj.createNode("add", "loose_point")
    add.parm("points").set(1)
    add.parmTuple("pt0").set((0, 1, 0))
    add.parm("usept0").set(1)
    return add


def packed_box(obj):
    box = obj.createNode("box", "box")
    box.parmTuple("size").set((0.3, 0.3, 0.3))
    pack = obj.createNode("pack", "pack")
    pack.setInput(0, box)
    return pack


merged("open_polygon", -6, -1, open_polygon)
merged("loose_point", -2, -1, loose_point)
merged("packed_primitive", 2, -1, packed_box)

shaded, wave = mesh_object("shaded", 6, -1)
vertex = shaded.createNode("attribwrangle", "vertex_uv_and_normals")
vertex.setInput(0, wave)
vertex.parm("class").set(3)  # vertices
vertex.parm("snippet").set(
    "v@uv = set(@P.x * 0.3 + 0.5, @P.z * 0.3 + 0.5, 0); v@N = normalize(set(0.2 * @P.x, 1, 0.1 * @P.z));"
)
colour = shaded.createNode("attribwrangle", "detail_colour")
colour.setInput(0, vertex)
colour.parm("class").set(0)  # detail
colour.parm("snippet").set("v@Cd = set(0.2 + 0.1 * (@Frame % 3), 0.5, 0.7);")
output(shaded, colour)


def invalid(name, x, level, snippet, code):
    """A mesh whose attribute is damaged by a Python SOP after it is written."""
    obj, wave = mesh_object(name, x, -4.5)
    written = obj.createNode("attribwrangle", "written")
    written.setInput(0, wave)
    written.parm("class").set(level)
    written.parm("snippet").set(snippet)
    damage = obj.createNode("python", "invalid_values")
    damage.setInput(0, written)
    damage.parm("python").set("geo = hou.pwd().geometry()\n" + code)
    output(obj, damage)
    return obj


# Vertex v of quad q is linear vertex 4 * q + v on these grids; quad 400 comes
# before quad 12000 in triangle order and lands in another parallel task.
NORMAL = "v@N = normalize(set(0.2 * @P.x, 1, 0.1 * @P.z));"
NORMAL_DAMAGE = """values = list(geo.vertexFloatAttribValues("N"))
zero, bad = 3 * (4 * %d + 1), 3 * (4 * %d + 2)
values[zero : zero + 3] = [0.0, 0.0, 0.0]
values[bad] = float("nan")
geo.setVertexFloatAttribValues("N", values)
"""
invalid(
    "invalid_uv",
    -6,
    3,
    "v@uv = set(@P.x * 0.3 + 0.5, @P.z * 0.3 + 0.5, 0);",
    """values = list(geo.vertexFloatAttribValues("uv"))
values[3 * (4 * 12000 + 1)] = float("nan")
values[3 * (4 * 400 + 2) + 1] = float("inf")
geo.setVertexFloatAttribValues("uv", values)
""",
)
invalid("invalid_normal_zero_first", -2, 3, NORMAL, NORMAL_DAMAGE % (400, 12000))
invalid("invalid_normal_nan_first", 2, 3, NORMAL, NORMAL_DAMAGE % (12000, 400))
invalid_colour = invalid(
    "invalid_colour",
    6,
    2,
    "v@Cd = set(0.3, 0.6, 0.2);",
    """values = list(geo.pointFloatAttribValues("Cd"))
values[3 * 17000 + 2] = float("inf")
values[3 * 5000] = float("nan")
geo.setPointFloatAttribValues("Cd", values)
""",
)

camera = scene.createNode("cam", "camera")
camera.setParms(dict(tx=0, ty=12, tz=1, rx=-90, projection=1, orthowidth=17))
material = ch.create_material(name="mesh_parallel_shaded", shader="emission")
emission = material.node("surface_shader")
emission.parm("strength").set(1)
attribute = material.createNode("cycles::attribute::1", "colour")
attribute.parm("attribute").set("Cd")
emission.setNamedInput("color", attribute, "color")
shaded.parm("shop_materialpath").set(material.path())
invalid_colour.parm("shop_materialpath").set(material.path())
# invalid_uv keeps the default surface: its uv stream is read and dropped all the
# same. A second keyed material, or a material with several links, would make
# the payload digests differ between processes (material keys are per process).
rop = menu_actions.create_rop()
device_selection.set_devices(rop, [device])
rop.setParms(
    dict(
        objectroot=scene.path(),
        vobject="*",
        camera=camera.path(),
        resx=width,
        resy=height,
        samples=2,
        ipr_samples=2,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_texture_cache=0,
        render_threads=2,
        world_source="color",
        allowmotionblur=0,
    )
)
rop.parmTuple("background").set((0.1, 0.1, 0.1))

steps = [1, 2, 3, 4, 5, 2]
counters = (
    "mesh_triangulated_faces",
    "mesh_retained_faces",
    "mesh_topology_builds",
    "mesh_topology_reuses",
    "mesh_topology_scans",
    "partition_scans",
    "partition_cache_hits",
    "geometry_resources",
    "extractions",
    "cache_hits",
)

# Every scene the session sends is also encoded whole (no acknowledged base).
# Material keys and 32-digit identities hash process-specific node ids:
# --record also keeps a hash with them blanked, comparable between two builds
# run in separate processes.
payloads, portable = [], []
MATERIAL_KEY = re.compile(rb"native-material:[0-9a-f]+")
IDENTITY = re.compile(rb"(?<= \x00\x00\x00\x00\x00\x00\x00)[0-9a-f]{32}")


class PayloadRecorder:
    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        value = getattr(object.__getattribute__(self, "_inner"), name)
        if name != "encode_snapshot":
            return value

        def encode(desired, *rest):
            whole = value(desired)
            payloads.append(hashlib.sha256(whole).hexdigest())
            blanked = IDENTITY.sub(b"#" * 32, MATERIAL_KEY.sub(lambda match: b"#" * len(match.group(0)), whole))
            portable.append(hashlib.sha256(blanked).hexdigest())
            return value(desired, *rest)

        return encode


queued._native = PayloadRecorder(queued._native)


def run(serial):
    if serial:
        os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
    else:
        os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    hou.setFrame(1)
    records = []
    with ch.Session(
        device=device,
        width=width,
        height=height,
        samples=2,
        threads=2,
        collect_diagnostics=True,
        execution="worker",
    ) as session:
        for frame in steps:
            hou.setFrame(frame)
            del payloads[:], portable[:]
            result = session.wait(session.apply(native_scene.capture(rop), reuse_if_unchanged=True))
            status = session.status()
            assert not status["error"] and status["state"] == "complete", (frame, status)
            plane = result["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"])
            assert np.isfinite(pixels).all(), frame
            records.append(
                dict(
                    frame=frame,
                    image=hashlib.sha256(plane["pixels"]).hexdigest(),
                    coverage=int((pixels[:, :, 3] > 0.5).sum()),
                    payloads=list(payloads),
                    portable=list(portable),
                    counters={key: status.get(key) for key in counters},
                    diagnostics=json.loads(json.dumps(status.get("source_diagnostics"), sort_keys=True, default=str)),
                )
            )
    os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
    return records


try:
    serial, parallel = run(True), run(False)
    report["steps"] = serial
    for a, b in zip(serial, parallel, strict=True):
        label = "frame %d" % a["frame"]
        assert a["payloads"], label
        assert a["payloads"] == b["payloads"], label
        assert a["image"] == b["image"], label
        assert a["counters"] == b["counters"], (label, a["counters"], b["counters"])
        assert a["diagnostics"] == b["diagnostics"], label
    # The steps reach the paths under test: moved quads are triangulated
    # again, the rebuilt Subdivide output is compared and partitioned again.
    first, later = serial[0]["counters"], serial[-1]["counters"]
    assert later["mesh_topology_scans"] >= len(steps) - 1, later
    assert later["partition_scans"] > first["partition_scans"] + len(steps) - 2, (first, later)
    assert later["mesh_triangulated_faces"] > first["mesh_triangulated_faces"] + 5 * 20000, (first, later)
    assert later["mesh_topology_reuses"] >= 5 * (len(steps) - 1), later
    assert serial[0]["coverage"] > 3000, serial[0]["coverage"]
    assert len({record["image"] for record in serial[:5]}) == 5, "frames must differ"
    # The invalid meshes report their attribute; each normal mesh reports its first reason.
    reported = json.dumps(serial[0]["diagnostics"])
    for expected in (
        "Cd contains a non-finite",
        "N contains a zero-length vector",
        "N contains a non-finite component",
    ):
        assert expected in reported, (expected, reported[:3000])
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2, default=str))
    if arguments.record:
        with open(arguments.record, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, default=str)
    print(json.dumps(dict(result=report["result"], error=report.get("error")), default=str), flush=True)
assert report["result"] == "passed", report.get("error")
