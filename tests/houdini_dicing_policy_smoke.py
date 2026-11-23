# SPDX-License-Identifier: Apache-2.0
"""Object opt-in, scene multipliers, native offscreen dicing and separate cameras."""

from support.paths import artifacts_root

import argparse
import json
import os
from pathlib import Path
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties, registry
from shader_bypass_preview_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
destination = Path(
    os.environ.get(
        "CH_DICING_REPORT",
        artifacts_root() / "stage2/subdivision/dicing-policy.json",
    )
)
destination.parent.mkdir(parents=True, exist_ok=True)
f = Fixture(args.device)
f.output.setNamedInput("surface", f.emission, "emission")
f.emission.parmTuple("color").set((0.2, 0.4, 0.8))
other = f.scene.createNode("geo", "unsubdivided", run_init_scripts=False)
box = other.createNode("box")
box.setRenderFlag(True)
box.setDisplayFlag(True)
other.parm("tx").set(20)
other.parm("shop_materialpath").set(f.builder.path())
dicing = f.scene.createNode("cam", "dicing_camera")
dicing.setParms(dict(tz=5, projection=1, orthowidth=4))
object_properties.install(f.geo)
report = dict(result="running", device=args.device, checks=[])
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))


def render(label):
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()), timeout=120)
    status = entry.session.status()
    assert not status["error"], status
    assert not status["source_diagnostics"], status["source_diagnostics"]
    f.expect(frame, (0.2, 0.4, 0.8))
    report["checks"].append(
        dict(
            name=label,
            triangles=status["subdivision_triangles"],
            tessellations=status["subdivision_tessellations"],
            conversions=status["geometry_conversions"],
        )
    )
    return frame, status


try:
    for name in (
        "subdivision_type",
        "subdivision_space",
        "subdivision_rate",
        "subdivision_level",
        "subdivision_boundary",
        "subdivision_fvar",
    ):
        assert f.rop.parm(name) is None, name
    assert f.rop.evalParm("dicing_rate") == 1 and f.rop.evalParm("ipr_dicing_rate") == 8
    assert f.rop.evalParm("offscreen_dicing_scale") == 4 and f.rop.evalParm("max_subdivisions") == 12
    assert f.rop.evalParm("limit_subdivision") == 0
    assert f.rop.parm("subdivision_limit").isDisabled()
    _, plain = render("Changing scene dicing policy never opts objects into subdivision")
    assert plain["subdivision_control_faces"] == 0
    f.rop.setParms(dict(dicing_rate=0.5, ipr_dicing_rate=4, max_subdivisions=16))
    assert all(r["subdivision"]["type"] == "none" for r in native_scene.capture(f.rop)["geometry"])
    _, plain = render("Even maximum depth 16 leaves unsubdivided objects as polygons")
    assert plain["subdivision_control_faces"] == 0

    f.geo.setParms(
        dict(
            cycles_subdivision_override=1,
            cycles_subdivision_type="catmull_clark",
            cycles_subdivision_adaptive=0,
            cycles_subdivision_ipr_level=1,
            cycles_subdivision_render_level=2,
        )
    )
    f.rop.parm("max_subdivisions").set(0)
    _, uniform = render("Uniform object levels remain independent of the adaptive scene limit")
    assert uniform["subdivision_control_faces"] == 1 and uniform["subdivision_triangles"] == 8, uniform
    final = native_scene.geometry_for_rop(f.rop, f.sop, hou.time(), interactive=False)["subdivision"]
    assert final["max_level"] == 2 and not final["adaptive"], final
    f.geo.parm("cycles_subdivision_ipr").set(0)
    assert native_scene.geometry_for_rop(f.rop, f.sop, hou.time())["subdivision"]["type"] == "none"
    assert (
        native_scene.geometry_for_rop(f.rop, f.sop, hou.time(), interactive=False)["subdivision"]["type"]
        == "catmull_clark"
    )
    f.geo.setParms(dict(cycles_subdivision_ipr=1, cycles_subdivision_adaptive=1, cycles_subdivision_pixel_size=0.5))
    f.rop.parm("max_subdivisions").set(8)
    preview = native_scene.geometry_for_rop(f.rop, f.sop, hou.time())["subdivision"]
    final = native_scene.geometry_for_rop(f.rop, f.sop, hou.time(), interactive=False)["subdivision"]
    assert preview["dicing_rate"] == 2 and final["dicing_rate"] == 0.25 and preview["triangle_limit"] == 0, (
        preview,
        final,
    )
    f.geo.setParms(dict(cycles_subdivision_space="object", cycles_subdivision_edge_length=0.1))
    assert np.isclose(native_scene.geometry_for_rop(f.rop, f.sop, hou.time())["subdivision"]["dicing_rate"], 0.4)
    f.geo.parm("cycles_subdivision_space").set("pixel")
    f.rop.setParms(dict(ipr_dicing_rate=2, offscreen_dicing_scale=1, max_subdivisions=8))
    _, onscreen = render("Object pixel size and edge length multiply the selected scene dicing rate")

    f.rop.parm("offscreen_dicing_scale").set(4)
    _, offscreen = render("Native offscreen scale reduces tessellation outside the view")
    assert offscreen["subdivision_triangles"] < onscreen["subdivision_triangles"], (onscreen, offscreen)
    f.rop.parm("offscreen_dicing_scale").set(1)
    _, restored = render("Restoring offscreen scale restores the same mesh density")
    assert restored["subdivision_triangles"] == onscreen["subdivision_triangles"]

    f.rop.parm("dicing_camera").set(dicing.path())
    _, fixed = render("A separate dicing camera with matching projection retains geometry density")
    before = fixed["subdivision_tessellations"]
    conversions = fixed["geometry_conversions"]
    f.camera.parm("tx").set(0.2)
    header = native_scene.capture(f.rop, camera_only=True)
    assert "dicing" not in header, "Camera-only navigation must not reacquire a fixed dicing camera"
    frame = entry.session.wait(entry.apply(header, hou.time()), timeout=120)
    assert entry.session.status()["subdivision_tessellations"] == before
    f.expect(frame, (0.2, 0.4, 0.8))
    dicing.parm("orthowidth").set(8)
    _, coarser = render("Editing the dicing camera changes density without changing the rendered camera")
    assert coarser["subdivision_triangles"] < fixed["subdivision_triangles"]
    assert coarser["geometry_conversions"] == conversions
    f.rop.parm("dicing_camera").set("")
    _, following = render("Clearing Dicing Camera returns to following the render camera")
    f.camera.parm("tx").set(0.3)
    entry.session.wait(entry.apply(native_scene.capture(f.rop, camera_only=True), hou.time()), timeout=120)
    assert entry.session.status()["subdivision_tessellations"] > following["subdivision_tessellations"]
    f.rop.parm("dicing_camera").set(f.camera.path())
    assert native_scene.dicing_configuration(f.rop, hou.time())["camera_path"] == ""

    f.rop.setParms(dict(max_subdivisions=0, dicing_camera=""))
    _, zero = render("Adaptive maximum zero is a valid coarsest tessellation, not a global object enable")
    assert zero["subdivision_control_faces"] == 1
    f.rop.parm("max_subdivisions").set(16)
    _, maximum = render("Adaptive depth 16 is accepted without assuming every face reaches the limit")
    assert maximum["subdivision_triangles"] > zero["subdivision_triangles"]

    f.rop.setParms(dict(limit_subdivision=1, subdivision_limit=1))
    try:
        entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()), timeout=120)
    except RuntimeError as error:
        assert "Subdivision triangle budget" in str(error), str(error)
    else:
        raise AssertionError("Explicit triangle budget did not fail")
    f.rop.parm("limit_subdivision").set(0)
    _, repaired = render("Disabling an explicit budget repairs rendering instead of capping quality")
    assert repaired["subdivision_triangles"] == maximum["subdivision_triangles"]
    grid = f.sop.inputs()[0].inputs()[0]
    grid.setParms(dict(rows=4, cols=4))
    f.rop.parm("max_subdivisions").set(12)
    _, many = render("Default adaptive depth 12 accepts more than seven control faces")
    assert many["subdivision_control_faces"] == 9, many
    pack = f.geo.createNode("pack", "adaptive_packed")
    pack.setInput(0, f.sop)
    pack.setRenderFlag(True)
    pack.setDisplayFlag(True)
    _, packed = render("Packed geometry obeys the same explicit object subdivision policy")
    assert packed["subdivision_control_faces"] == 9, packed
    f.geo.parm("cycles_subdivision_override").set(0)
    _, disabled = render("Disabling object subdivision restores original polygons")
    assert disabled["subdivision_control_faces"] == 0
    report.update(result="passed", execution=entry.session.execution)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
