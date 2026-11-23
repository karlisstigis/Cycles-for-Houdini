# SPDX-License-Identifier: Apache-2.0
"""Native override ownership on faces, points, hair, packed instances and mixed SOPs."""

import argparse, json, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import registry
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
f.rop.setParms(dict(use_material_assignments=1, samples=8, ipr_samples=8, point_radius=0.3, curve_width=0.2))
colors = [(0.8, 0.1, 0.05), (0.05, 0.7, 0.1)]
base = (0.2, 0.3, 0.4)


def builder(name, color):
    node = ch.create_material(name=name, shader="emission")
    shader = node.node("surface_shader")
    shader.parmTuple("color").set(color)
    shader.parm("strength").set(1)
    ch.promote_parameter(node, shader, "color", name="tint")
    return node


source_material = builder("override_binding_source", base)
blue = builder("override_embedded", (0.05, 0.1, 0.8))
for node in (source_material, blue):
    ch.promote_parameter(node, node.node("surface_shader"), "strength", name="power")


def assign(source, name, style="primitive"):
    node = f.obj.createNode("material", name)
    node.setInput(0, source)
    node.setParms(dict(style=style, num_materials=2))
    for index, color in enumerate(colors, 1):
        node.setParms(
            {
                f"group{index}": str(index - 1),
                f"shop_materialpath{index}": source_material.path(),
                f"num_local{index}": 1,
                f"local{index}_name1": "tint",
                f"local{index}_type1": "color",
            }
        )
        node.parmTuple(f"local{index}_cval1").set(color)
    return node


def output(node):
    node.setDisplayFlag(True)
    node.setRenderFlag(True)


entry = f.session()
report = dict(result="running", device=f.device, checks=[])
dest = f.dest.parent / "material-override-bindings"
dest.mkdir(exist_ok=True)


def render(label, expected=colors, code=None, y=0, geometry=None, packed=None):
    frame = f.update()
    status = entry.session.status()
    pixels = f.pixels(frame)
    rows = []
    for x, color in zip((-1, 1), expected):
        column = int((x / 4 + 0.5) * frame["width"])
        row = int((y / 4 + 0.5) * frame["height"])
        actual = pixels[row, column, :3]
        np.testing.assert_allclose(actual, color, atol=1e-5, rtol=1e-5)
        rows.append(actual.tolist())
    assert status["state"] == "complete" and status["render_device_ids"] == [f.device], status
    assert {d["code"] for d in status["source_diagnostics"]} == ({code} if code else set()), status
    if geometry is not None:
        assert status["geometry_resources"] == geometry, status
    if packed is not None:
        assert status["packed_instances"] == packed, status
    refs = entry.session.native_material_references()
    report["checks"].append(dict(label=label, rgb=rows, status=status, references=refs))
    return frame


try:
    f.sop.setParms(dict(rows=2, cols=3))
    f.sop.parmTuple("size").set((4, 1))
    mesh = assign(f.sop, "face_overrides")
    output(mesh)
    render("Two faces retain independent overrides in one mesh", geometry=1)
    assert len(entry.session.native_material_references()) == 2
    pid = entry.session.status()["renderer_pid"]
    mesh.parm("local1_name1").set("missing")
    render(
        "A broken face override does not invalidate its neighbor",
        (base, colors[1]),
        "material.override_ignored",
        geometry=1,
    )
    mesh.parm("local1_name1").set("tint")
    render("Face override repair clears its warning", geometry=1)
    targets = f.obj.createNode("add", "native_points")
    targets.setParms(dict(points=2, usept0=1, pt0x=-1, usept1=1, pt1x=1))
    points = assign(targets, "point_overrides", "point")
    output(points)
    render("Native point-owner overrides shade loose spheres independently", geometry=1)
    targets.parm("addparticlesystem").set(1)
    render("The same overrides work on a particle primitive", geometry=1)
    parent = f.obj.createNode("material", "particle_primitive_material")
    parent.setInput(0, targets)
    parent.parm("shop_materialpath1").set(blue.path())
    points.setInput(0, parent)
    inherited = f.obj.createNode("attribwrangle", "inherit_particle_material")
    inherited.setInput(0, points)
    inherited.parm("class").set(2)
    inherited.parm("snippet").set('s@shop_materialpath="";')
    output(inherited)
    render("Point overrides retain the containing primitive material identity", geometry=1)
    assert {r["path"] for r in entry.session.native_material_references()} == {blue.path()}
    lines = []
    for index, x in enumerate((-1, 1)):
        line = f.obj.createNode("line", "native_curve_" + str(index))
        line.setParms(dict(points=4, dist=1.2))
        line.parmTuple("origin").set((x, -0.6, 0))
        lines.append(line)
    curves_source = f.obj.createNode("merge", "native_curves")
    for index, line in enumerate(lines):
        curves_source.setInput(index, line)
    curves = assign(curves_source, "curve_overrides")
    output(curves)
    render("Whole-curve primitive overrides shade hair independently", geometry=1)
    invalid = f.obj.createNode("attribwrangle", "uniform_key_override")
    invalid.setInput(0, curves)
    invalid.parm("class").set(2)
    invalid.parm("snippet").set("s@material_override=\"{'tintr':0.5}\";")
    output(invalid)
    render("Uniform key overrides retain the complete curve shader", ((0.5, 0.1, 0.05), (0.5, 0.7, 0.1)), geometry=1)
    output(curves)
    render("Removing the uniform key layer restores primitive shading", geometry=1)
    box = f.obj.createNode("box", "native_packed_prototype")
    box.parmTuple("size").set((0.8, 1, 0.5))
    copies = f.obj.createNode("copytopoints", "native_packed_copies")
    copies.setInput(0, box)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    packed = assign(copies, "packed_overrides")
    output(packed)
    render("Different packed fallback overrides create explicit variants", geometry=2, packed=2)
    packed.parmTuple("local2_cval1").set(colors[0])
    render("Identical packed bindings share their prototype", (colors[0], colors[0]), geometry=1, packed=2)
    packed.parmTuple("local2_cval1").set(colors[1])
    render("Packed binding repair retains both instances", geometry=2, packed=2)
    embedded = f.obj.createNode("material", "embedded_prototype_assignment")
    embedded.setInput(0, box)
    embedded.parm("shop_materialpath1").set(blue.path())
    copies.setInput(0, embedded)
    render(
        "Fully assigned prototypes ignore a different outer material and its overrides",
        ((0.05, 0.1, 0.8),) * 2,
        geometry=1,
        packed=2,
    )
    assert {r["path"] for r in entry.session.native_material_references()} == {blue.path()}
    # One Houdini object can contain all supported ownership contexts together.
    targets.parm("addparticlesystem").set(0)
    for name in ("pt0y", "pt1y"):
        targets.parm(name).set(1.2)
    for line, x in zip(lines, (-1, 1)):
        line.parmTuple("origin").set((x, -1.6, 0))
        line.parm("dist").set(0.8)
    combined = f.obj.createNode("merge", "mixed_native_bindings")
    for index, node in enumerate((mesh, points, curves)):
        combined.setInput(index, node)
    output(combined)
    frame = render("Mixed mesh point and curve components keep their valid overrides", geometry=3)
    pixels = f.pixels(frame)
    for y in (-1.2, 1.2):
        for x, color in zip((-1, 1), colors):
            np.testing.assert_allclose(
                pixels[int((y / 4 + 0.5) * 64), int((x / 4 + 0.5) * 64), :3], color, atol=1e-5, rtol=1e-5
            )
    # A point path can agree with one primitive and switch away from another.
    # Both contain the same override literal; only the agreeing path inherits it.
    particles = f.obj.createNode("merge", "independent_particle_primitives")
    for index, x in enumerate((-1, 1)):
        one = f.obj.createNode("add", "particle_" + str(index))
        one.setParms(dict(points=1, usept0=1, pt0x=x, addparticlesystem=1))
        particles.setInput(index, one)
    primitive = f.obj.createNode("attribwrangle", "particle_parent_bindings")
    primitive.setInput(0, particles)
    primitive.setParms(
        dict(
            **{"class": 1},
            snippet='s@shop_materialpath=@primnum==0?"'
            + source_material.path()
            + '":"'
            + blue.path()
            + '";s@material_override="{\'power\':3}";',
        )
    )
    point_binding = f.obj.createNode("attribwrangle", "particle_effective_bindings")
    point_binding.setInput(0, primitive)
    point_binding.setParms(dict(**{"class": 2}, snippet='s@shop_materialpath="' + source_material.path() + '";'))
    output(point_binding)
    render(
        "Identical point paths retain the distinct containing primitive context",
        (tuple(np.array(base) * 3), base),
        geometry=1,
    )
    primitive.parm("snippet").set(
        's@shop_materialpath="' + source_material.path() + '";s@material_override="{\'power\':3}";'
    )
    render("Repairing primitive paths restores inherited point overrides", (tuple(np.array(base) * 3),) * 2, geometry=1)
    # Switching away and back must not resurrect an older packed fallback.
    detail = f.obj.createNode("attribwrangle", "prototype_detail_binding")
    detail.setInput(0, box)

    def detail_binding(node):
        detail.setParms(
            dict(
                **{"class": 0}, snippet='s@shop_materialpath="' + node.path() + '";s@material_override="{\'power\':3}";'
            )
        )

    leaf = f.obj.createNode("attribwrangle", "prototype_face_binding")
    leaf.setInput(0, detail)

    def leaf_binding(node, literal=""):
        leaf.setParms(
            dict(
                **{"class": 1},
                snippet='s@shop_materialpath="' + node.path() + '";s@material_override="' + literal + '";',
            )
        )

    for name in ("pt0y", "pt1y"):
        targets.parm(name).set(0)
    copies.setInput(0, leaf)
    output(packed)
    detail_binding(blue)
    leaf_binding(source_material)
    render(
        "Switching back to the packed outer path does not restore its discarded overrides",
        (base, base),
        geometry=1,
        packed=2,
    )
    detail_binding(source_material)
    render(
        "An agreeing detail path retains supported packed layers",
        tuple(tuple(np.array(color) * 3) for color in colors),
        geometry=2,
        packed=2,
    )
    leaf_binding(blue, "{'power':4}")
    render("A different face path uses only its own override", ((0.2, 0.4, 3.2),) * 2, geometry=1, packed=2)
    detail_binding(blue)
    leaf_binding(source_material)
    render("Repeated packed material switches restore the intended base binding", (base, base), geometry=1, packed=2)
    assert entry.session.status()["renderer_pid"] == pid
    report.update(result="passed")
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
