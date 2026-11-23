# SPDX-License-Identifier: Apache-2.0
"""Real whole-curve population/byte boundaries and live local repairs."""

import pass_fixtures as pass_fixture

import ast
import json

import hou
import numpy as np
import cycles_houdini as ch


CURVE_CODE = """import hou
n=hou.pwd();g=n.geometry();count=n.evalParm('count');distinct=n.evalParm('distinct');large=n.evalParm('large')
d=g.addAttrib(hou.attribType.Global,'material_override','')
g.setGlobalAttribValue(d,"{'tint':(0.2,0.4,0.6)}" if not large else "{'power':3}")
p=g.addAttrib(hou.attribType.Prim,'material_override','')
v=g.addAttrib(hou.attribType.Vertex,'material_override','')
w=g.addAttrib(hou.attribType.Point,'width',.025)
p=g.findPrimAttrib('material_override');v=g.findVertexAttrib('material_override')
assert p is not None and v is not None
for i in range(count):
 c=g.createPolygon(is_closed=False);c.setAttribValue(p,"{'power':2}" if not large else '')
 value=repr({'image_file':'x'*30000+'_'+str(i),'power':4}) if large else repr({'power':3+(i%distinct)*.000001})
 for y in (-.5,.5):
  q=g.createPoint();q.setPosition((-1.8+3.6*i/max(1,count-1),y,0));q.setAttribValue(w,.025)
  c.addVertex(q).setAttribValue(v,value)
"""


def verify_curve_capacity(f, entry, report, material, byte_material, dest, devices):
    source = f.obj.createNode("python", "whole_curve_capacity")
    for name, default in (("count", 4097), ("distinct", 4097), ("large", 0)):
        source.addSpareParmTuple(hou.IntParmTemplate(name, name, 1, default_value=(default,)))
    pass_fixture.parm(source, "python").set(CURVE_CODE)
    original_material = pass_fixture.evaluate(f.obj, "shop_materialpath")
    original_position = pass_fixture.evaluate(f.rop, "pass_position")
    pass_fixture.parm(f.rop, "pass_position").set(1)
    pass_fixture.parm(f.obj, "shop_materialpath").set(material.path())
    source.setDisplayFlag(True)
    source.setRenderFlag(True)

    control = f.scene.createNode("geo", "curve_capacity_height_control", run_init_scripts=False)
    grid = control.createNode("grid", "unchanged_plane")
    pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((1.2, 0.5))
    grid.parmTuple("t").set((0, -1.3, 0))
    grid.setDisplayFlag(True)
    grid.setRenderFlag(True)
    control_material = ch.create_material(name="curve_capacity_control", shader="emission")
    control_material.node("surface_shader").parmTuple("color").set((0.07, 0.11, 0.19))
    pass_fixture.parm(control_material.node("surface_shader"), "strength").set(1)
    height = control_material.createNode("cycles::vector_displacement::1", "independent_height")
    pass_fixture.set_parameters(height, dict(space="object", scale=1, midlevel=0))
    height.parmTuple("vector").set((0, 0, 0.15))
    control_material.node("surface_output").setNamedInput("displacement", height, "displacement")
    pass_fixture.parm(control_material, "cycles_displacement_method").set("true")
    pass_fixture.parm(control, "shop_materialpath").set(control_material.path())

    first_pid = None

    def check(label, expected=None, *, warning=False, byte_budget=False, boundary=False, cached=False):
        nonlocal first_pid
        before = entry.session.status()
        report["pending"] = label
        (dest / "report.json").write_text(json.dumps(report, indent=2))
        if cached:
            pass_fixture.parm(f.camera, "tx").set(0.01)
        frame = f.update()
        status = entry.session.status()
        pixels = f.pixels(frame)
        positions = f.pixels(frame, "position")
        assert np.isfinite(pixels).all() and np.isfinite(positions).all()
        # Film can be opaque even on the background; use interior raster
        # samples rather than interpreting zero-valued position as geometry.
        body = np.zeros(pixels.shape[:2], dtype=bool)
        body[30:34, 30:34] = True
        witness = (
            (pixels[:, :, 3] > 0.999) & (np.abs(positions[:, :, 0]) < 0.3) & (np.abs(positions[:, :, 1] + 1.3) < 0.15)
        )
        assert body.sum() >= 8 and witness.sum() >= 8, (label, int(body.sum()), int(witness.sum()))
        observed = pixels[:, :, :3][body]
        refs = [
            r
            for r in entry.session.native_material_references()
            if r["path"] == pass_fixture.evaluate(f.obj, "shop_materialpath")
        ]
        issues = status["source_diagnostics"]
        report["current"] = dict(
            label=label,
            status=status,
            curve_references=len(refs),
            rgb=observed.mean(0).tolist(),
            witness_z=positions[:, :, 2][witness].tolist(),
        )
        (dest / "report.json").write_text(json.dumps(report, indent=2))
        assert status["state"] == "complete" and not status["error"], status
        assert status["render_device_ids"] == devices and status["curves"] == pass_fixture.evaluate(source, "count"), (
            status
        )
        assert status["geometry_resources"] == status["object_resources"] == 2, status
        assert {d["code"] for d in issues} == ({"material.override_capacity"} if warning else set()), issues
        assert len(issues) == int(warning), issues
        if warning:
            assert "vertex override layer" in issues[0]["action"], issues
            if byte_budget:
                assert "8 MiB" in issues[0]["message"], issues
        if first_pid is None:
            first_pid = status["renderer_pid"]
        assert status["renderer_pid"] == first_pid
        if cached:
            assert status["geometry_conversions"] == before["geometry_conversions"], (before, status)
            assert status["extractions"] == before["extractions"], (before, status)
        if boundary:
            assert len(refs) == 4096, len(refs)
            ratios = observed / np.array((0.2, 0.4, 0.6))
            assert np.all((ratios >= 2.99998) & (ratios <= 3.00412)), ratios
            np.testing.assert_allclose(ratios[:, 0], ratios[:, 1], atol=3e-5, rtol=0)
            np.testing.assert_allclose(ratios[:, 1], ratios[:, 2], atol=3e-5, rtol=0)
        else:
            np.testing.assert_allclose(observed, np.broadcast_to(expected, observed.shape), atol=3e-5, rtol=0)
            assert len(refs) == 1, len(refs)
            if not byte_budget:
                effective = {}
                for layer in refs[0]["overrides"]:
                    effective.update(ast.literal_eval(layer))
                assert effective["tint"] == (0.2, 0.4, 0.6) and effective["power"] == (2 if warning else 3), effective
        np.testing.assert_allclose(
            pixels[:, :, :3][witness], np.broadcast_to((0.07, 0.11, 0.19), (int(witness.sum()), 3)), atol=3e-5, rtol=0
        )
        np.testing.assert_allclose(positions[:, :, 2][witness], 0.15, atol=3e-5, rtol=0)
        report["checks"].append(report.pop("current"))
        (dest / "report.json").write_text(json.dumps(report, indent=2))

    try:
        check(
            "4097 whole-curve vertex overrides retain primitive/detail and mesh height", (0.4, 0.8, 1.2), warning=True
        )
        check("Curve capacity warning survives camera-only cache reuse", (0.4, 0.8, 1.2), warning=True, cached=True)
        pass_fixture.parm(source, "distinct").set(1)
        check("Curve vertex population repaired in the same IPR worker", (0.6, 1.2, 1.8))
        pass_fixture.parm(source, "distinct").set(4097)
        check("Curve capacity failure recurs locally", (0.4, 0.8, 1.2), warning=True)
        pass_fixture.parm(source, "distinct").set(1)
        check("Repeated curve repair clears omission policy", (0.6, 1.2, 1.8))
        pass_fixture.parm(source, "distinct").set(4096)
        check("Exactly 4096 whole-curve overrides remain supported", boundary=True)
        pass_fixture.parm(source, "distinct").set(1)
        check("Large supported curve palette retires after repair", (0.6, 1.2, 1.8))
        # A constant 1x1 image has independent valid coordinates on curves;
        # excessive filename overrides must be rejected before opening files.
        texture = byte_material.node("byte_color")
        coordinates = byte_material.createNode("cycles::geometry::1", "curve_byte_coordinates")
        texture.setNamedInput("vector", coordinates, "position")
        pass_fixture.parm(f.obj, "shop_materialpath").set(byte_material.path())
        pass_fixture.set_parameters(source, dict(count=300, large=1))
        check(
            "Whole-curve literals over 8 MiB preserve the valid lower image",
            (0.6, 1.2, 1.8),
            warning=True,
            byte_budget=True,
        )
        pass_fixture.parm(source, "python").set(
            CURVE_CODE.replace(
                "value=repr({'image_file':'x'*30000+'_'+str(i),'power':4}) if large else",
                "value=repr({'power':4}) if large else",
            )
        )
        check("Whole-curve byte budget repairs while IPR stays active", (0.8, 1.6, 2.4), byte_budget=True)
    finally:
        control.destroy()
        pass_fixture.parm(f.obj, "shop_materialpath").set(original_material)
        pass_fixture.parm(f.rop, "pass_position").set(original_position)
        pass_fixture.parm(f.camera, "tx").set(0)
