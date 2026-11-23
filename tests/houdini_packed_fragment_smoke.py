# SPDX-License-Identifier: Apache-2.0
"""Packed fragments render as the same pieces unpacked and as packed geometry.

One fractured object (four named pieces, two materials, inner and outer shader
fields) is rendered three ways in one session: as packed fragments (Pack by
name), as packed geometry (the same Pack with fragments off) and unpacked by
Houdini's own Unpack SOP. Images must agree for static pieces, per-piece
rotations about their centroid pivots, velocity and angular velocity blur,
nested packing in both orders, a deforming source, and fragments deleted and
added in the live session. A transform-only frame change must convert no
geometry and build no fragment, exactly as for packed geometry. Fragments that
select nothing produce one clear diagnostic and render nothing.
"""

import argparse
import json
import os
import re
import traceback
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, native_scene
from support.paths import artifacts_root

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
# Every built fragment is compared with the HDK's own fragment range.
os.environ["CYCLES_HOUDINI_CHECK_FRAGMENTS"] = "1"
os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION", None)
dest = artifacts_root() / "stage3/packed-fragments" / re.sub(r"[^a-z0-9]+", "_", device.lower())
dest.mkdir(parents=True, exist_ok=True)
width, height = 192, 96
report = dict(result="running", device=device, checks=[])

hou.setFps(24)
hou.setFrame(1)
scene = hou.node("/obj").createNode("subnet", "packed_fragment_fixture")
obj = scene.createNode("geo", "fractured", run_init_scripts=False)
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=6, projection=1, orthowidth=5.2))

material_a = ch.create_material(name="fragment_inner_outer", shader="emission")
material_b = ch.create_material(name="fragment_second", shader="emission")


def field_shader(material, x=None, z=None):
    emission = material.node("surface_shader")
    emission.parm("strength").set(1)
    combine = material.createNode("cycles::combine_xyz::1", "fields")
    for name in ("inner", "outer"):
        node = material.createNode("cycles::attribute::1", name)
        node.parm("attribute").set(name)
    outer = material.node("outer")
    inner = material.node("inner")
    combine.setNamedInput("y", outer, "fac")
    if x is None:
        combine.setNamedInput("x", inner, "fac")
    else:
        combine.parm("x").set(x)
    if z is None:
        uv = material.createNode("cycles::attribute::1", "uv")
        uv.parm("attribute").set("uv")
        split = material.createNode("cycles::separate_xyz::1", "uv_u")
        split.setNamedInput("vector", uv, "vector")
        combine.setNamedInput("z", split, "x")
    else:
        combine.setNamedInput("z", inner, "fac")
    emission.setNamedInput("color", combine, "vector")


field_shader(material_a)
field_shader(material_b, x=0.9, z="inner")

empty = obj.createNode("add", "nothing")
source = obj.createNode("attribwrangle", "four_pieces")
source.setInput(0, empty)
source.parm("class").set(source.parm("class").menuItems().index("detail"))
source.parm("snippet").set(
    """addprimattrib(0, "name", ""); addprimattrib(0, "inner", 0.0);
addprimattrib(0, "shop_materialpath", ""); addpointattrib(0, "uv", {0,0,0});
string a = "%s", b = "%s";
for (int k = 0; k < 4; k++) for (int j = 0; j < 4; j++) {
    float x0 = -1.9 + 1.0 * k + 0.32 * (j %% 2), y0 = -0.32 + 0.32 * (j / 2);
    int p0 = addpoint(0, set(x0, y0, 0)), p1 = addpoint(0, set(x0, y0 + 0.32, 0));
    int p2 = addpoint(0, set(x0 + 0.32, y0 + 0.32, 0)), p3 = addpoint(0, set(x0 + 0.32, y0, 0));
    int corners[] = array(p0, p1, p2, p3);
    foreach (int p; corners) setpointattrib(0, "uv", p, set(0.1 + 0.2 * (j %% 2) + 0.05 * k, 0, 0));
    int f = addprim(0, "poly", p0, p1, p2, p3);
    setprimattrib(0, "name", f, sprintf("piece%%d", k));
    setprimattrib(0, "inner", f, 0.15 + 0.2 * k + 0.04 * j);
    // Piece 0 holds both materials; pieces 1-2 use a; piece 3 uses b.
    setprimattrib(0, "shop_materialpath", f, k == 0 ? (j %% 2 ? b : a) : (k == 3 ? b : a));
}"""
    % (material_a.path(), material_b.path())
)
deform = obj.createNode("attribwrangle", "deforming_source")
deform.setInput(0, source)
deform.parm("snippet").set("@P.y += 0.12 * sin(@Time * 9 + @P.x * 3);")
deform.bypass(True)
pack = obj.createNode("pack", "pack_by_name")
pack.setInput(0, deform)
pack.setParms(dict(packbyname=1, nameattribute="name", packedfragments=1, pivot="centroid"))
packed = obj.createNode("pack", "pack_geometry_by_name")
packed.setInput(0, deform)
packed.setParms(dict(packbyname=1, nameattribute="name", packedfragments=0, pivot="centroid"))


def placement(node, name):
    result = obj.createNode("attribwrangle", name)
    result.setInput(0, node)
    result.parm("class").set(result.parm("class").menuItems().index("point"))
    for parm in ("angle", "spin"):
        result.addSpareParmTuple(hou.FloatParmTemplate(parm, parm, 1))
    result.addSpareParmTuple(hou.FloatParmTemplate("move", "move", 3))
    result.addSpareParmTuple(hou.IntParmTemplate("blur", "blur", 1))
    result.addSpareParmTuple(hou.IntParmTemplate("keep", "keep", 1, default_value=(99,)))
    result.parm("snippet").set(
        """addprimattrib(0, "outer", 0.0);
setprimattrib(0, "outer", @ptnum, 0.1 + 0.15 * @ptnum);
matrix3 m = ident();
rotate(m, radians(chf("angle") * (@ptnum + 1) + chf("spin") * @Time), {0, 0, 1});
setprimintrinsic(0, "transform", @ptnum, m);
@P += chv("move") * (@ptnum + 1);
if (chi("blur")) { v@v = set(12.0 * (@ptnum + 1), 0, 0); v@w = set(0, 0, 24.0); }
if (@ptnum >= chi("keep")) removeprim(0, @ptnum, 1);"""
    )
    return result


fragments = placement(pack, "fragment_pieces")
geometry = placement(packed, "geometry_pieces")
for parm in ("angle", "spin", "movex", "movey", "movez", "blur", "keep"):
    geometry.parm(parm).set(fragments.parm(parm))
unpacked = obj.createNode("unpack", "houdini_unpack")
unpacked.setInput(0, fragments)
unpacked.parm("transfer_attributes").set("outer")

# Nested: fragments inside a packed geometry primitive.
outer_pack = obj.createNode("pack", "fragments_in_packed_geometry")
outer_pack.setInput(0, fragments)
outer_pack.parm("pivot").set("origin")


def unpack_twice(node, name):
    # One level at a time: Unpack transfers attributes only from the level it opens.
    first = obj.createNode("unpack", name + "_outer_level")
    first.setInput(0, node)
    second = obj.createNode("unpack", name)
    second.setInput(0, first)
    second.parm("transfer_attributes").set("outer")
    return second


outer_unpacked = unpack_twice(outer_pack, "unpack_fragments_in_geometry")
# Nested: fragments whose shared geometry holds packed geometry primitives.
inner = obj.createNode("attribwrangle", "inner_packed_fields")
inner.setInput(0, packed)
inner.parm("class").set(inner.parm("class").menuItems().index("primitive"))
inner.parm("snippet").set('f@outer = 0.1 + 0.15 * @primnum; s@group = sprintf("pair%d", @primnum / 2);')
pairs = obj.createNode("pack", "fragments_of_packed_geometry")
pairs.setInput(0, inner)
pairs.setParms(dict(packbyname=1, nameattribute="group", packedfragments=1, pivot="centroid"))
pairs_unpacked = unpack_twice(pairs, "unpack_fragments_of_geometry")

# Fragments that select nothing: a value no primitive holds, and an attribute
# the shared geometry lacks. Their neighbours keep rendering.
broken = obj.createNode("python", "fragments_selecting_nothing")
broken.parm("python").set(
    """geo = hou.pwd().geometry()
shared = hou.Geometry()
shared.merge(hou.node(%r).geometry())
geo.createPacked("PackedFragment").setEmbeddedGeometry(shared, "name", "no_such_piece")
geo.createPacked("PackedFragment").setEmbeddedGeometry(shared, "no_such_attribute", "piece0")"""
    % source.path()
)
with_broken = obj.createNode("merge", "valid_and_empty_fragments")
with_broken.setInput(0, fragments)
with_broken.setInput(1, broken)

choices = [fragments, geometry, unpacked, outer_pack, outer_unpacked, pairs, pairs_unpacked, with_broken]
output = obj.createNode("switch", "OUT")
for i, node in enumerate(choices):
    output.setInput(i, node)
output.setRenderFlag(True)
output.setDisplayFlag(True)
obj.layoutChildren()

rop = menu_actions.create_rop()
device_selection.set_devices(rop, [device])
rop.setParms(
    dict(
        objectroot=scene.path(),
        vobject="*",
        lights="",
        camera=camera.path(),
        resx=width,
        resy=height,
        samples=16,
        ipr_samples=16,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        film_exposure=1,
        allowmotionblur=0,
        use_texture_cache=0,
        render_threads=2,
        world_source="color",
    )
)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
for node in [source, pack, packed] + choices:
    try:
        node.cook(force=True)
    except hou.OperationFailed:
        pass
    assert not node.errors(), (node.path(), node.errors())
names = [p.intrinsicValue("fragmentname") for p in pack.geometry().prims()]
assert [p.type().name() for p in pack.geometry().prims()] == ["PackedFragment"] * 4, pack.geometry().prims()
assert [p.type().name() for p in packed.geometry().prims()] == ["PackedGeometry"] * 4
assert names == ["piece%d" % k for k in range(4)], names


def compare(label, image, reference, mean=2e-3, outliers=0.004):
    diff = np.abs(image - reference).max(axis=2)
    result = dict(label=label, mean=float(diff.mean()), max=float(diff.max()), outliers=float((diff > 0.05).mean()))
    assert result["mean"] <= mean and result["outliers"] <= outliers, result
    return result


try:
    with ch.Session(
        device=device, width=width, height=height, samples=16, threads=2, collect_diagnostics=True
    ) as session:

        def render(label, choice, diagnostics=0, at=None):
            if at is not None:
                hou.setFrame(at)
            output.parm("input").set(choices.index(choice))
            frame = session.wait(session.apply(native_scene.capture(rop)))
            status = session.status()
            assert not status["error"] and status["state"] == "complete", status
            issues = status["source_diagnostics"]
            assert len(issues) == diagnostics, (label, issues)
            plane = frame["planes"]["combined"]
            image = np.frombuffer(plane["pixels"], np.float32).reshape(height, width, plane["channels"]).copy()
            assert np.isfinite(image).all()
            magenta = np.all(np.isclose(image[:, :, :3], (1, 0, 1), atol=0.01), axis=2) & (image[:, :, 3] > 0.99)
            assert not magenta.any(), (label, "missing shader field")
            np.save(str(dest / (label.replace(" ", "_") + ".npy")), image)
            row = dict(
                label=label,
                covered=int((image[:, :, 3] > 0.5).sum()),
                conversions=status["geometry_conversions"],
                poses=status.get("adapter_pose_updates"),
                full=status.get("adapter_full_object_updates"),
                fragments=status["packed_fragments"],
                diagnostics=issues,
            )
            report["checks"].append(row)
            return image, row

        def same(label, choice, reference_choice, mean=2e-3, outliers=0.004, **kwargs):
            image, row = render(label + " fragments", choice, **kwargs)
            reference, _ = render(label + " reference", reference_choice)
            report["checks"].append(compare(label, image, reference, mean, outliers))
            assert row["covered"] > 1000, row
            return image, row

        def three(label, exact=True):
            image, row = same(label + " vs packed geometry", fragments, geometry, *((1e-5, 0) if exact else ()))
            same(label + " vs unpack", fragments, unpacked)
            return image, row

        three("Static pieces")
        fragments.parm("angle").set(25)
        fragments.parmTuple("move").set((0.05, 0.08, 0))
        three("Rotated about centroid pivots")
        same("Fragments inside packed geometry", outer_pack, outer_unpacked)
        same("Packed geometry inside fragments", pairs, pairs_unpacked)
        # Transform-only frame change: no fragment build, no geometry conversion.
        fragments.parm("spin").set(40)
        for choice, label in ((geometry, "packed geometry"), (fragments, "fragments")):
            _, first = render("Spin frame 1 " + label, choice, at=1)
            _, second = render("Spin frame 2 " + label, choice, at=2)
            assert second["conversions"] == first["conversions"], (label, first, second)
            if choice is fragments:
                for key in ("fragments_built", "partitions", "fallback_ranges"):
                    assert second["fragments"][key] == first["fragments"][key], (key, first, second)
                transform_only = dict(first=first, second=second)
        same("Spin frame 2 vs unpack", fragments, unpacked, at=2)
        report["transform_only"] = transform_only
        hou.setFrame(1)
        fragments.parm("spin").set(0)
        # Velocity and angular velocity blur from the packed points.
        rop.setParms(dict(allowmotionblur=1, geometry_motion="velocity"))
        fragments.parm("blur").set(1)
        blurred, _ = same("Velocity and angular blur vs packed geometry", fragments, geometry, 1e-5, 0)
        fragments.parm("blur").set(0)
        sharp, _ = render("Blur source without velocity", fragments)
        changed = float((np.abs(blurred - sharp).max(axis=2) > 0.05).mean())
        assert changed > 0.02, ("velocity blur had no effect", changed)
        rop.parm("allowmotionblur").set(0)
        # Deforming shared geometry: rebuilt each frame, matching Unpack.
        deform.bypass(False)
        built = None
        for at in (1, 3):
            _, row = same("Deforming source frame %d" % at, fragments, unpacked, at=at)
            if built is not None:
                assert row["fragments"]["fragments_built"] > built, row
            built = row["fragments"]["fragments_built"]
        deform.bypass(True)
        hou.setFrame(1)
        # Live deletion and addition of fragments.
        for keep in (2, 4, 1, 3, 99):
            fragments.parm("keep").set(keep)
            three("Keep %d pieces" % keep)
        # Fragments selecting nothing: one diagnostic, neighbours unchanged.
        image, row = render("Fragments selecting nothing", with_broken, diagnostics=1)
        reference, _ = render("Valid fragments only", fragments)
        report["checks"].append(compare("Empty fragments render nothing", image, reference, 1e-5, 0))
        issue = row["diagnostics"][0]
        assert issue["code"] == "geometry.packed_unavailable", issue
        assert "matches no primitive" in issue["message"] or "no_such_piece" in json.dumps(issue), issue
        # Same result from a serial build of freshly cooked fragments.
        os.environ["CYCLES_HOUDINI_SERIAL_CONVERSION"] = "1"
        pack.parm("pivot").set("origin")
        pack.parm("pivot").set("centroid")
        serial, _ = render("Serial fragments", fragments)
        os.environ.pop("CYCLES_HOUDINI_SERIAL_CONVERSION")
        parallel, _ = render("Parallel fragments", fragments)
        report["checks"].append(compare("Serial and parallel builds agree", serial, parallel, 1e-6, 0))
        # CPU renders of the same scene are bitwise equal; GPU renders differ in the
        # last float bit between sessions (scheduling), so allow that there.
        assert report["checks"][-1]["max"] <= (0 if device == "CPU" else 1e-6), report["checks"][-1]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2, default=str))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
