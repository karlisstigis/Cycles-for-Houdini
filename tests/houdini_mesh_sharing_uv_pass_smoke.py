# SPDX-License-Identifier: Apache-2.0
"""UV output preserves each logical binding's field absence across live pass edits."""

from support.paths import artifacts_root
import argparse, json, os, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
dest = artifacts_root() / "stage3/mesh-sharing"
dest.mkdir(parents=True, exist_ok=True)
path = dest / "uv-report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[])
root = hou.node("/obj").createNode("subnet", "sharing_uv")
sources = []
for index in range(2):
    obj = root.createNode("geo", "mesh_" + str(index), run_init_scripts=False)
    obj.parm("tx").set((index * 2 - 1) * 1.1)
    grid = obj.createNode("grid", "shape")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((2, 2))
    clean = obj.createNode("attribwrangle", "remove_uv")
    clean.setInput(0, grid)
    clean.parm("class").set(clean.parm("class").menuItems().index("detail"))
    clean.parm("snippet").set(
        'removeattrib(0,"point","uv");removeattrib(0,"vertex","uv");removeattrib(0,"prim","uv");removeattrib(0,"detail","uv");'
    )
    field = obj.createNode("attribwrangle", "uv_field")
    field.setInput(0, clean)
    field.parm("snippet").set("v@uv=set(.7,.2,0);" if index else "")
    field.setDisplayFlag(True)
    field.setRenderFlag(True)
    sources.append((obj, field))
assert sources[0][1].geometry().findPointAttrib("uv") is None
np.testing.assert_allclose(
    np.asarray(sources[1][1].geometry().pointFloatAttribValues("uv")).reshape(-1, 3),
    np.broadcast_to((0.7, 0.2, 0), (4, 3)),
    rtol=0,
    atol=3e-6,
)
camera = root.createNode("cam", "camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=5))
width, height = 160, 64
base = ["combined", "position", "depth"]


def scene(passes):
    return dict(
        geometry=[
            ch.geometry_request(obj, sop, material="material/" + str(i), material_mode="uniform")
            for i, (obj, sop) in enumerate(sources)
        ],
        materials=[
            dict(key="material/0", kind="emission", color=(0.8, 0.1, 0.2)),
            dict(key="material/1", kind="emission", color=(0.1, 0.2, 0.8)),
        ],
        camera_path=camera.path(),
        background=(0, 0, 0),
        passes=passes,
    )


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(height, width, p["channels"])


try:
    with ch.Session(
        device=device,
        width=width,
        height=height,
        samples=1,
        threads=2,
        execution="worker",
        interactive=False,
        use_texture_cache=False,
    ) as session:
        for enabled, authored in (
            [(value, False) for value in (False, True, False, True, False, True, False)]
            + [(value, True) for value in (False, True, False, True, False)]
            + [(True, False), (False, False)]
        ):
            sources[0][1].parm("snippet").set("v@uv=set(.7,.2,0);" if authored else "")
            frame = session.wait(session.apply(scene(base + (["uv"] if enabled else [])), reuse_if_unchanged=True))
            status = session.status()
            assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
            pos = plane(frame, "position")
            depth = plane(frame, "depth")[:, :, 0]
            values = []
            for index in range(2):
                mask = (
                    (depth > 0)
                    & (depth < 100)
                    & (np.abs(pos[:, :, 0] - (index * 2 - 1) * 1.1) < 0.6)
                    & (np.abs(pos[:, :, 1]) < 0.6)
                )
                assert mask.sum() > 20
                if enabled:
                    uv = plane(frame, "uv")[:, :, :2][mask]
                    expected = (0.7, 0.2) if index or authored else (0, 0)
                    np.testing.assert_allclose(
                        uv,
                        np.broadcast_to(expected, uv.shape),
                        atol=3e-5,
                        rtol=0,
                        err_msg="UV ownership for mesh " + str(index),
                    )
                    values.append(uv.mean(0).tolist())
            assert status["mesh_variant_resources"] == (2 if enabled and not authored else 1), status
            report["checks"].append(dict(enabled=enabled, authored=authored, uv=values, status=status))
            path.write_text(json.dumps(report, indent=2))
    report.update(result="passed", closed=session.status())
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
