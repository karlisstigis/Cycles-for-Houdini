# SPDX-License-Identifier: Apache-2.0
"""Distinct per-primitive stylesheet source validation, native matching and repair."""

from support.paths import artifacts_root
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, native_scene

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
os.environ["CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"] = "1"
dest = artifacts_root() / "stage3/stylesheet-storage/attribute-acquisition"
dest.mkdir(parents=True, exist_ok=True)
scene = hou.node("/obj").createNode("subnet", "stylesheet_attributes")
obj = scene.createNode("geo", "receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=65, cols=129))
grid.parmTuple("size").set((8, 4))
source = obj.createNode("python", "distinct_sources")
source.setInput(0, grid)
source.addSpareParmTuple(hou.ToggleParmTemplate("broken", "Broken Sources"))
source.parm("python").set("""import json
node=hou.pwd();g=node.geometry();a=g.addAttrib(hou.attribType.Prim,'material_stylesheet','')
for primitive in g.prims():
 i=primitive.number();gain=(i//227)%3+1
 value=json.dumps(dict(styles=[dict(target=dict(type='self'),overrides=dict(materialParameters=dict(tint=[.2*gain,.4*gain,.6*gain])))]))+' '*(i%256)+'\\n'*(i//256)
 primitive.setAttribValue(a,'{broken' if node.evalParm('broken') and i%227==0 else value)
""")
source.setDisplayFlag(True)
source.setRenderFlag(True)
material = ch.create_material(name="attribute_styles", shader="emission")
shader = material.node("surface_shader")
shader.parmTuple("color").set((0.2, 0.4, 0.6))
shader.parm("strength").set(1)
ch.promote_parameter(material, shader, "color", name="tint")
obj.parm("shop_materialpath").set(material.path())
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
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        render_threads=2,
        world_source="color",
        use_texture_cache=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
report = dict(result="running", device=device, checks=[], scope=__doc__)
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
        for broken in (False, True, False):
            source.parm("broken").set(broken)
            g = source.geometry()
            owners = g.prims()
            positions = np.asarray(
                [
                    tuple(sum((v.point().position() for v in p.vertices()), hou.Vector3()) / len(p.vertices()))
                    for p in owners
                ]
            )
            gains = np.asarray(
                [(p.number() // 227) % 3 + 1 if not (broken and p.number() % 227 == 0) else 1 for p in owners]
            )
            xs, ys = np.unique(positions[:, 0]), np.unique(positions[:, 1])
            lookup = np.zeros((len(ys), len(xs)))
            lookup[np.searchsorted(ys, positions[:, 1]), np.searchsorted(xs, positions[:, 0])] = gains
            update = native_scene.capture(rop)
            update["passes"] = ["combined", "position", "depth"]
            started = time.perf_counter()
            frame = session.wait(session.apply(update), timeout=180)
            status = session.status()

            def plane(name):
                value = frame["planes"][name]
                return np.frombuffer(value["pixels"], np.float32).reshape(height, width, value["channels"])

            rgb = plane("combined")[:, :, :3]
            position = plane("position")
            depth = plane("depth")[:, :, 0]
            assert np.isfinite(rgb).all() and not status["error"] and bool(status["source_diagnostics"]) == broken, (
                status
            )
            xx = (position[:, :, 0] - xs[0]) / (xs[1] - xs[0])
            yy = (position[:, :, 1] - ys[0]) / (ys[1] - ys[0])
            ix = np.rint(xx).astype(int).clip(0, len(xs) - 1)
            iy = np.rint(yy).astype(int).clip(0, len(ys) - 1)
            mask = (
                (depth > 0)
                & (depth < 100)
                & (np.abs(position[:, :, 0]) < 3.9)
                & (np.abs(position[:, :, 1]) < 1.9)
                & (np.abs(xx - np.rint(xx)) < 0.43)
                & (np.abs(yy - np.rint(yy)) < 0.43)
            )
            expected = lookup[iy, ix, None] * np.asarray((0.2, 0.4, 0.6))
            assert mask.sum() > 200
            np.testing.assert_allclose(rgb[mask], expected[mask], atol=3e-4, rtol=0)
            report["checks"].append(
                dict(
                    broken=broken,
                    owners=len(owners),
                    pixels=int(mask.sum()),
                    apply_to_frame_seconds=time.perf_counter() - started,
                    status=status,
                    diagnostics=session.diagnostics(),
                )
            )
            capture = report["checks"][-1]["diagnostics"]["capture"]["events"][-1]
            assert capture["preparation_yields"] >= 2, capture
            assert capture["maximum_preparation_seconds"] < capture["preparation_seconds"] * 0.5, capture
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
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
