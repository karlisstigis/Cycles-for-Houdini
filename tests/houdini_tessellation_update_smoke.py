# SPDX-License-Identifier: Apache-2.0
"""Automatic tessellation policy, one-shot refresh and original-polygon startup."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import traceback
import hou
import numpy as np
from cycles_houdini import native_scene, object_properties, registry, image_iris
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
dest = Path(os.environ.get("CH_TESSELLATION_REPORT", artifacts_root() / "tessellation-update/report.json"))
dest.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
f = Fixture(args.device)
coords = f.builder.createNode("cycles::texture_coordinate::1", "gradient")
f.emission.setNamedInput("color", coords, "generated")
f.output.setNamedInput("surface", f.emission, "emission")
object_properties.install(f.geo)
f.geo.setParms(dict(cycles_subdivision_override=1, cycles_subdivision_type="linear", cycles_subdivision_adaptive=1))
f.rop.setParms(dict(ipr_dicing_rate=2, max_subdivisions=8, offscreen_dicing_scale=1))
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))


def render(label, camera_only=False):
    frame = entry.session.wait(
        entry.apply(native_scene.capture(f.rop, camera_only=camera_only), hou.time()), timeout=120
    )
    status = entry.session.status()
    assert not status["error"], status["error"]
    assert not status["source_diagnostics"], [
        (issue, hou.node(issue["source"]).errors() if hou.node(issue["source"]) else ())
        for issue in status["source_diagnostics"]
    ]
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()
    assert np.isfinite(image).all()
    report["checks"].append(
        dict(
            name=label,
            triangles=status["subdivision_triangles"],
            tessellations=status["subdivision_tessellations"],
            conversions=status["geometry_conversions"],
        )
    )
    return status, image


def retained(before, after):
    for key in ("subdivision_tessellations", "subdivision_triangles", "geometry_conversions"):
        assert before[key] == after[key], (key, before[key], after[key])


def refresh(label):
    f.rop.parm("refresh_tessellation").pressButton()
    result = render(label)
    assert not f.rop.evalParm("tessellation_update"), "One-shot refresh changed the automatic policy"
    return result


try:
    assert image_iris.supported()
    parm = f.rop.parm("tessellation_update")
    assert parm is not None and parm.eval() and f.rop.parm("freeze_adaptive_subdivision") is None
    assert parm.parmTemplate().label() == "Adaptive Subdivision Update"
    assert parm.containingFolders() == ("Rendering", "IPR Settings"), parm.containingFolders()
    baseline, original = render("Default on calculates adaptive tessellation")
    assert baseline["subdivision_triangles"] > 2
    parm.set(0)
    held, _ = render("Disabling updates retains cached tessellation")
    retained(baseline, held)
    f.camera.setParms(dict(tx=0.5, orthowidth=8))
    moved, image = render("Camera-only edits retain cached tessellation", True)
    retained(held, moved)
    assert np.max(np.abs(image - original)) > 0.01
    f.geo.parm("sx").set(0.5)
    scaled, _ = render("Object movement retains cached tessellation")
    retained(held, scaled)
    refreshed, _ = refresh("Refresh while off updates current camera and object transforms once")
    assert refreshed["subdivision_tessellations"] == scaled["subdivision_tessellations"] + 1
    assert refreshed["subdivision_triangles"] < scaled["subdivision_triangles"]
    same, _ = render("An unchanged refresh revision does not repeat tessellation")
    retained(refreshed, same)
    f.geo.parm("sx").set(2)
    moved, _ = render("Object-only movement after refresh retains its result")
    retained(refreshed, moved)
    parm.set(1)
    automatic, _ = render("Re-enabling updates applies object-only movement")
    assert automatic["subdivision_triangles"] > moved["subdivision_triangles"]
    reference = registry.session_from_configuration(native_scene.configuration(f.rop))
    try:
        reference.wait(reference.apply(native_scene.capture(f.rop)), timeout=120)
        assert reference.status()["subdivision_triangles"] == automatic["subdivision_triangles"]
    finally:
        reference.close()
    dicing = f.scene.createNode("cam", "test_dicing_camera")
    dicing.setParms(dict(tz=5, projection=1, orthowidth=8))
    f.rop.parm("dicing_camera").set(dicing.path())
    fixed, _ = render("Separate dicing camera")
    parm.set(0)
    render("Disable updates for separate dicing camera")
    dicing.parm("orthowidth").set(16)
    fixed_move, _ = render("Separate dicing camera edits stay held")
    retained(fixed, fixed_move)
    fixed_refresh, _ = refresh("Explicit refresh uses the current separate dicing camera")
    assert fixed_refresh["subdivision_triangles"] < fixed_move["subdivision_triangles"]
    grid = f.sop.inputs()[0].inputs()[0]
    grid.setParms(dict(rows=3, cols=3))
    edited, _ = render("Invalidated source cache renders original polygons without adaptive work")
    assert edited["subdivision_control_faces"] == 4 and edited["subdivision_triangles"] == 8, edited
    assert edited["subdivision_tessellations"] == fixed_refresh["subdivision_tessellations"]
    rebuilt, _ = refresh("Refresh subdivides the edited source")
    assert (
        rebuilt["subdivision_triangles"] > 8
        and rebuilt["subdivision_tessellations"] > edited["subdivision_tessellations"]
    )
    registry.close_all()
    entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
    restarted, _ = render("Fresh IPR with updates off starts with the original mesh")
    assert restarted["subdivision_triangles"] == 8 and restarted["subdivision_tessellations"] == 0, restarted
    f.camera.parm("tx").set(0.75)
    restart_move, _ = render("Camera movement does not tessellate an uncached mesh", True)
    retained(restarted, restart_move)
    restart_refresh, _ = refresh("Refresh builds the first tessellation with updates still off")
    assert restart_refresh["subdivision_tessellations"] == 1 and restart_refresh["subdivision_triangles"] > 8
    f.geo.setParms(dict(cycles_subdivision_space="object", cycles_subdivision_edge_length=0.5))
    space, _ = render("New object-space source also stays unsubdivided while off")
    assert space["subdivision_triangles"] == 8 and space["subdivision_tessellations"] == 1
    space_refresh, _ = refresh("Explicit refresh also handles object-space adaptive subdivision")
    assert space_refresh["subdivision_triangles"] > 8 and space_refresh["subdivision_tessellations"] == 2
    parm.set(1)
    f.rop.parm("dicing_camera").set("")
    f.geo.setParms(dict(sx=1, cycles_subdivision_space="pixel", cycles_subdivision_type="catmull_clark"))
    targets = f.geo.createNode("python", "instance_targets")
    targets.parm("python").set(
        "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'pscale',1.0)\nfor x,s in [(-1,.2),(1,.35)]:\n p=g.createPoint();p.setPosition((x,0,0));p.setAttribValue('pscale',s)"
    )
    packed = f.geo.createNode("copytopoints", "packed_instances")
    packed.setInput(0, f.sop)
    packed.setInput(1, targets)
    packed.parm("pack").set(1)
    packed.setDisplayFlag(True)
    packed.setRenderFlag(True)
    packed_baseline, _ = render("Packed Catmull-Clark instances own two pixel variants")
    assert packed_baseline["mesh_variant_resources"] == 2, packed_baseline
    parm.set(0)
    render("Disable packed adaptive updates")
    f.camera.parm("orthowidth").set(4)
    f.geo.parm("sx").set(2)
    packed_move, packed_image = render("Packed variants retain tessellation through navigation")
    retained(packed_baseline, packed_move)
    f.emission.parm("strength").set(0.5)
    shaded, shaded_image = render("Surface shading stays live on held packed meshes")
    retained(packed_move, shaded)
    assert np.max(np.abs(shaded_image - packed_image)) > 0.01
    packed_refresh, _ = refresh("Refresh updates both private packed variants")
    assert packed_refresh["subdivision_tessellations"] == shaded["subdivision_tessellations"] + 2
    assert packed_refresh["subdivision_triangles"] > shaded["subdivision_triangles"]
    registry.close_all()
    entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
    packed_start, _ = render("Fresh packed Catmull-Clark scene starts unsubdivided while off")
    assert packed_start["subdivision_tessellations"] == 0 and packed_start["subdivision_triangles"] == 16, packed_start
    f.geo.setParms(dict(cycles_subdivision_adaptive=0, cycles_subdivision_ipr_level=1))
    uniform, _ = render("Uniform subdivision remains independent of adaptive update policy")
    assert uniform["subdivision_tessellations"] > 0 and uniform["subdivision_triangles"] > 16
    # Compare a concave source with Houdini's independent polygon triangulation.
    polygon = f.geo.createNode("python", "concave_control_mesh")
    polygon.parm("python").set(
        "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Vertex,'uv',(0.,0.,0.));p=g.createPolygon()\nfor x,y in [(-1.5,-1.5),(1.5,-1.5),(1.5,-.5),(-.5,-.5),(-.5,1.5),(-1.5,1.5)]:\n v=g.createPoint();v.setPosition((x,y,0));c=p.addVertex(v);c.setAttribValue('uv',(x/4+.5,y/4+.5,0))"
    )
    polygon.setDisplayFlag(True)
    polygon.setRenderFlag(True)
    f.geo.setParms(dict(sx=1, cycles_subdivision_adaptive=1))
    f.camera.setParms(dict(tx=0, orthowidth=4))
    f.emission.setNamedInput("color", coords, "UV")
    concave, coarse_image = render("Concave original polygons preserve corner UVs without tessellation")
    assert concave["subdivision_triangles"] == 4
    f.geo.parm("cycles_subdivision_override").set(0)
    _, reference_image = render("Independent Houdini triangulation matches the original-polygon preview")
    assert np.max(np.abs(coarse_image - reference_image)) < 0.003
    normals = f.geo.createNode("python", "original_polygon_normals")
    normals.parm("python").set(
        "import hou,math\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'N',(0.,0.,1.))\n"
        "for x,y,z in [(-1,-1,0),(0,-1,.3),(1,-1,0),(-1,1,0),(0,1,.3),(1,1,0)]:\n"
        " p=g.createPoint();p.setPosition((x,y,z));n=hou.Vector3((.4*x,0,1)).normalized();p.setAttribValue('N',tuple(n))\n"
        "for indices in [(0,1,4,3),(1,2,5,4)]:\n"
        " face=g.createPolygon()\n"
        " for index in indices:face.addVertex(g.points()[index])\n"
    )
    normals.setDisplayFlag(True)
    normals.setRenderFlag(True)
    geometry = f.builder.createNode("cycles::geometry::1", "source_normals")
    f.emission.setNamedInput("color", geometry, "normal")
    for owner in ("point", "vertex"):
        if owner == "vertex":
            normals.parm("python").set(
                normals.parm("python").eval()
                + "\ng.addAttrib(hou.attribType.Vertex,'N',(0.,0.,1.))\n"
                + "for face in g.prims():\n"
                + " for vertex in face.vertices():vertex.setAttribValue('N',(.4,0.,math.sqrt(.84)) if face.number() else (0.,.4,math.sqrt(.84)))\n"
            )
        f.geo.parm("cycles_subdivision_override").set(0)
        plain, expected_normals = render("Authored " + owner + " normals on original polygons")
        f.geo.setParms(dict(cycles_subdivision_override=1, cycles_subdivision_type="catmull_clark"))
        preview, actual_normals = render("Disabled adaptive update preserves authored " + owner + " normals")
        assert preview["subdivision_tessellations"] == plain["subdivision_tessellations"]
        assert preview["subdivision_control_faces"] == 2 and preview["subdivision_triangles"] == 4
        normal_error = float(np.max(np.abs(actual_normals - expected_normals)))
        report["checks"][-1]["normal_max_error"] = normal_error
        assert normal_error < 0.0001, (owner, normal_error)
    f.rop.parm("tessellation_update").set(1)
    refined, limit_normals = render("Enabled Catmull-Clark derives limit normals with authored control normals")
    normals.parm("python").set(
        normals.parm("python").eval()
        + "\nfor attribute in (g.findPointAttrib('N'),g.findVertexAttrib('N')):\n"
        + " if attribute is not None:attribute.destroy()\n"
    )
    _, limit_reference = render("Catmull-Clark limit normals match a source without authored normals")
    limit_error = float(np.max(np.abs(limit_normals - limit_reference)))
    report["checks"][-1]["normal_max_error"] = limit_error
    assert limit_error < 0.0001, limit_error
    f.rop.parm("tessellation_update").set(0)
    held, held_normals = render("Disabling adaptive updates retains refined limit normals")
    assert held["subdivision_triangles"] == refined["subdivision_triangles"]
    assert np.max(np.abs(held_normals - limit_reference)) < 0.0001
    polygon.setDisplayFlag(True)
    polygon.setRenderFlag(True)
    f.geo.parm("cycles_subdivision_type").set("linear")
    f.emission.setNamedInput("color", coords, "UV")
    concave, _ = render("Return to the original concave polygon preview")
    from cycles_houdini import passes

    passes.add(f.rop, "position")
    displacement = f.builder.createNode("cycles::vector_displacement::1", "coarse_displacement")
    # The default this test was written for (new nodes now take Blender's).
    displacement.setParms(dict(scale=1.0, space="object"))
    displacement.parmTuple("vector").set((0, 0, 0.2))
    f.output.setNamedInput("displacement", displacement, "displacement")
    f.builder.parm("cycles_displacement_method").set("true")
    f.geo.parm("cycles_subdivision_override").set(1)
    displaced, displaced_image = render("True displacement evaluates original polygons without adaptive dicing")
    assert displaced["subdivision_tessellations"] == concave["subdivision_tessellations"]
    assert displaced["subdivision_triangles"] == 4
    position = np.frombuffer(entry.session.frame()["planes"]["position"]["pixels"], np.float32).reshape(64, 64, -1)
    # Three independently located interiors of the L-shaped control polygon.
    heights = position[[16, 16, 48], [16, 48, 16], 2]
    assert np.max(np.abs(heights - 0.2)) < 0.001, heights
    assert native_scene.dicing_configuration(f.rop, hou.time(), interactive=False)["update"]
    report.update(result="passed", image_iris=True, execution=entry.session.execution)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
