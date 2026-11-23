# SPDX-License-Identifier: Apache-2.0
"""Swap cached UV requirements between unlike meshes without changing geometry."""

from support.paths import artifacts_root
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from tangent_basis_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/attribute-layout"
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(args.device)
f.configure("normal_original")
f.rop.setParms(dict(samples=16, ipr_samples=16, allowmotionblur=0))
f.animate.parm("uv_speed").set(0)
left = f.objects[0]
left.parm("shop_materialpath").set(f.material.path())
shape = left.createNode("attribwrangle", "tilted_authored_normals")
shape.setInput(0, left.node("front_facing_grid"))
shape.parm("snippet").set(
    "vector p=@P; v@named_uv3=set(p.x+.5,p.y+.5,0); @P=set(.8*p.x,p.y,-.6*p.x); v@N=set(.6,0,.8);"
)
missing = left.createNode("attribdelete", "optional_uv")
missing.setInput(0, shape)
missing.setRenderFlag(True)
missing.setDisplayFlag(True)
r = dict(result="running", device=args.device, checks=[], scope=__doc__)
try:
    with ch.Session(
        device=args.device,
        width=f.width,
        height=f.height,
        samples=16,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:
        states = [(0, 0), (1, 0), (0, 1), (1, 1), (0, 0)] + [(n % 2, 1 - n % 2) for n in range(24)]
        for index, (broken_left, broken_right) in enumerate(states):
            missing.parm("ptdel").set(f.field if broken_left else "")
            f.edit(broken_right)
            frame = session.wait(session.apply(native_scene.capture(f.rop), at_time=hou.time()))
            p = f.planes(frame)
            status = session.status()
            assert status["state"] == "complete" and not status["error"], status
            assert len(status["source_diagnostics"]) == broken_left + broken_right, status["source_diagnostics"]
            assert status["geometry_resources"] == status["object_resources"] == 2, status
            assert all(np.isfinite(value).all() for value in p.values())
            if index == 0:
                baseline = p
                rebuilds = status["displacement_rebuilds"]
                for x, expected in ((-1, (0.6, 0, 0.8)), (1, (0, 0, 1))):
                    mask = (np.abs(p["position"][:, :, 0] - x) < 0.2) & (np.abs(p["position"][:, :, 1]) < 0.2)
                    assert mask.sum() > 20
                    np.testing.assert_allclose(
                        p["normal"][mask], np.broadcast_to(expected, p["normal"][mask].shape), rtol=0, atol=4e-5
                    )
            else:
                assert status["displacement_rebuilds"] == rebuilds, status
                for name in ("position", "normal"):
                    np.testing.assert_allclose(
                        p[name],
                        baseline[name],
                        rtol=0,
                        atol=3e-6,
                        err_msg=str((index, broken_left, broken_right, name)),
                    )
            r["checks"].append(dict(left_missing=broken_left, right_missing=broken_right, status=status))
        session.close(wait=True)
        closed = session.status()
        assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
        r.update(result="passed", closed=closed)
except BaseException:
    failure = dest / ("failure-" + str(time.time_ns()) + ".npz")
    if "p" in globals():
        np.savez(failure, **p)
    r.update(result="failed", error=traceback.format_exc(), failure_planes=str(failure))
    raise
finally:
    (dest / "report.json").write_text(json.dumps(r, indent=2))
print(json.dumps(dict(result=r["result"], checks=len(r["checks"]))))
