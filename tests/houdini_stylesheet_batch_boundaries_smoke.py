# SPDX-License-Identifier: Apache-2.0
"""Native per-owner CVEX values survive subject batch boundaries and repairs."""

from support.paths import artifacts_root
import argparse, json, os, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, native_scene

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
destination = artifacts_root() / "stage3/stylesheet-storage/boundaries"
destination.mkdir(parents=True, exist_ok=True)
scene = hou.node("/obj").createNode("subnet", "stylesheet_boundaries")
obj = scene.createNode("geo", "source", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=97, cols=129))
grid.parmTuple("size").set((8, 4))
source = obj.createNode("python", "owner_values")
source.setInput(0, grid)
source.addSpareParmTuple(hou.ToggleParmTemplate("points_only", "Points Only"))
source.addSpareParmTuple(hou.ToggleParmTemplate("broken", "Broken Values"))
source.parm("python").set("""node=hou.pwd();g=node.geometry();points=node.evalParm('points_only')
if points:g.deletePrims(g.prims(),keep_points=True)
elements=g.points() if points else g.prims();a=g.addAttrib(hou.attribType.Point if points else hou.attribType.Prim,'gain',0.)
for i,e in enumerate(elements):e.setAttribValue(a,float('nan') if node.evalParm('broken') and i%701==0 else float((i//701)%3+1))
""")
source.setDisplayFlag(True)
source.setRenderFlag(True)
material = ch.create_material(name="batch_values", shader="emission")
surface = material.node("surface_shader")
base = np.asarray((0.2, 0.4, 0.6))
surface.parmTuple("color").set(tuple(base))
surface.parm("strength").set(1)
ch.promote_parameter(material, surface, "strength", name="power")
obj.parm("shop_materialpath").set(material.path())
obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
program = {
    "code": "cvex gain_value(float gain=1;export float result=0){result=gain;}",
    "bindings": {"gain": "gain"},
    "return": "result",
}
obj.parm("shop_materialstylesheet").set(
    json.dumps(dict(styles=[dict(target={}, overrides=dict(materialParameters=dict(power=dict(script=program))))]))
)
camera = scene.createNode("cam")
camera.setParms(dict(tz=6, projection=1, orthowidth=10))
rop = menu_actions.create_rop()
device_selection.set_devices(rop, device)
width, height = 192, 128
rop.setParms(
    dict(
        objectroot=scene.path(),
        vobject="*",
        lights="",
        camera=camera.path(),
        resx=width,
        resy=height,
        samples=1,
        ipr_samples=1,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
        point_radius=0.019,
        world_source="color",
        render_threads=2,
    )
)
rop.parmTuple("background").set((0, 0, 0))
report = dict(result="running", device=device, checks=[])
try:
    with ch.Session(
        device=device,
        width=width,
        height=height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:
        for points in (False, True):
            source.parm("points_only").set(points)
            obj.parm("shop_materialstylesheet").set(
                json.dumps(
                    dict(
                        styles=[
                            dict(
                                target={"type": "point"} if points else {},
                                overrides=dict(materialParameters=dict(power=dict(script=program))),
                            )
                        ]
                    )
                )
            )
            for broken in (False, True, False):
                source.parm("broken").set(broken)
                geometry = source.geometry()
                owners = geometry.points() if points else geometry.prims()
                positions = np.asarray(
                    [
                        tuple(e.position())
                        if points
                        else tuple(sum((v.point().position() for v in e.vertices()), hou.Vector3()) / len(e.vertices()))
                        for e in owners
                    ]
                )
                gains = np.asarray(
                    geometry.pointFloatAttribValues("gain") if points else geometry.primFloatAttribValues("gain")
                )
                gains = np.where(np.isfinite(gains), gains, 1.0)
                xs, ys = np.unique(positions[:, 0]), np.unique(positions[:, 1])
                lookup = np.zeros((len(ys), len(xs)))
                lookup[np.searchsorted(ys, positions[:, 1]), np.searchsorted(xs, positions[:, 0])] = gains
                update = native_scene.capture(rop)
                update["passes"] = ["combined", "position", "depth"]
                frame = session.wait(session.apply(update), timeout=120)
                status = session.status()

                def plane(name):
                    value = frame["planes"][name]
                    return np.frombuffer(value["pixels"], np.float32).reshape(height, width, value["channels"])

                rgb = plane("combined")[:, :, :3]
                position = plane("position")
                depth = plane("depth")[:, :, 0]
                assert (
                    np.isfinite(rgb).all() and not status["error"] and bool(status["source_diagnostics"]) == broken
                ), status
                xx = (position[:, :, 0] - xs[0]) / (xs[1] - xs[0])
                yy = (position[:, :, 1] - ys[0]) / (ys[1] - ys[0])
                ix = np.rint(xx).astype(int).clip(0, len(xs) - 1)
                iy = np.rint(yy).astype(int).clip(0, len(ys) - 1)
                mask = (
                    (depth > 0) & (depth < 100) & (np.abs(position[:, :, 0]) < 3.9) & (np.abs(position[:, :, 1]) < 1.9)
                )
                if not points:
                    mask &= (np.abs(xx - np.rint(xx)) < 0.43) & (np.abs(yy - np.rint(yy)) < 0.43)
                assert mask.sum() > 200, int(mask.sum())
                expected = lookup[iy, ix, None] * base
                np.testing.assert_allclose(rgb[mask], expected[mask], atol=3e-4, rtol=0)
                report["checks"].append(
                    dict(points=points, broken=broken, owners=len(owners), pixels=int(mask.sum()), status=status)
                )
        session.close(wait=True)
        closed = session.status()
        assert (
            closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0 and closed["state"] == "closed"
        ), closed
        report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
