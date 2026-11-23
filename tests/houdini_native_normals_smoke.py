# SPDX-License-Identifier: Apache-2.0
"""Native SOP normals and UVs versus independent Lambertian irradiance."""

from support.paths import artifacts_root
import json, math, os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch

obj = hou.node("/obj").createNode("geo", "native_normal_receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((2, 2))
outward = obj.createNode("reverse", "positive_z_receiver")
outward.setInput(0, grid)
assert np.allclose(tuple(outward.geometry().prims()[0].normal()), (0, 0, 1))
normal = obj.createNode("normal")
normal.setInput(0, outward)
uv = obj.createNode("attribwrangle")
uv.setInput(0, normal)
uv.setParms({"class": 3, "snippet": "v@uv=set(.5+.5*@P.x,.5+.5*@P.y,0);"})
camera = hou.node("/obj").createNode("cam", "native_normal_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
material = dict(key="receiver", kind="diffuse", color=(1, 1, 1), roughness=0)
light = dict(
    key="point", kind="point", strength=(100, 100, 100), normalize=True, transform=(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 5)
)
report = dict(result="running", cases=[])
for device in ("CPU", ch.default_device()):
    obj.parm("sx").set(1)
    with ch.Session(device=device, width=96, height=64, samples=512, interactive=False) as session:
        for scale in (1, -1):
            obj.parm("sx").set(scale)
            request = ch.geometry_request(obj, uv, material="receiver")
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=[request],
                        materials=[material],
                        lights=[light],
                        camera_path=camera.path(),
                        background=(0, 0, 0),
                    )
                )
            )
            beauty = (
                np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
                .reshape(64, 96, 4)[31:33, 47:49, :3]
                .mean(axis=(0, 1))
            )
            expected = 100 / (4 * math.pi**2 * 25)
            assert np.max(np.abs(beauty - expected)) < 0.001, (device, scale, beauty, expected)
            normals = np.frombuffer(frame["planes"]["normal"]["pixels"], np.float32).reshape(64, 96, 3)[31:33, 47:49, :]
            assert np.allclose(normals, (0, 0, 1), atol=0.001), (device, scale, normals)
            texcoords = (
                np.frombuffer(frame["planes"]["uv"]["pixels"], np.float32)
                .reshape(64, 96, 3)[31:33, 47:49, :2]
                .mean(axis=(0, 1))
            )
            assert np.allclose(texcoords, (0.5, 0.5), atol=0.001), (device, scale, texcoords)
            report["cases"].append(
                dict(device=device, scale=scale, beauty=beauty.tolist(), expected=expected, uv=texcoords.tolist())
            )
        # A face-forward normal pass alone cannot prove the winding. Independently
        # inspect the shader's geometric front/back classification as well.
        front_material = dict(
            key="receiver",
            kind="graph",
            graph=dict(
                nodes=[
                    dict(id="geometry", definition="cycles/geometry/1", parameters={}),
                    dict(id="emission", definition="cycles/emission/1", parameters=dict(color=[1, 1, 1])),
                ],
                links=[
                    {"from": dict(node="geometry", socket="backfacing"), "to": dict(node="emission", socket="strength")}
                ],
                surface=dict(node="emission", socket="emission"),
            ),
        )
        for scale in (1, -1):
            obj.parm("sx").set(scale)
            frame = session.wait(
                session.apply(
                    dict(geometry=[ch.geometry_request(obj, uv, material="receiver")], materials=[front_material])
                )
            )
            backfacing = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)[
                31:33, 47:49, :3
            ]
            assert np.max(np.abs(backfacing)) < 1e-5, (device, scale, backfacing)
            report["cases"].append(
                dict(
                    device=device,
                    scale=scale,
                    name="Native outward face is front-facing",
                    backfacing=backfacing.tolist(),
                )
            )
        obj.parm("sx").set(1)
        height_material = dict(
            key="receiver",
            kind="graph",
            graph=dict(
                nodes=[
                    dict(id="emission", definition="cycles/emission/1", parameters=dict(color=[1, 1, 1], strength=1)),
                    dict(
                        id="height",
                        definition="cycles/displacement/1",
                        parameters=dict(height=0.3, midlevel=0, scale=1, space="object"),
                    ),
                ],
                surface=dict(node="emission", socket="emission"),
                displacement=dict(node="height", socket="displacement"),
                displacement_method="true",
            ),
        )
        frame = session.wait(
            session.apply(
                dict(geometry=[ch.geometry_request(obj, outward, material="receiver")], materials=[height_material])
            )
        )
        position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(64, 96, 3)[31:33, 47:49, 2]
        assert np.max(np.abs(position - 0.3)) < 1e-5, (device, position)
        report["cases"].append(
            dict(device=device, name="Scalar displacement follows native +Z without authored N", z=position.tolist())
        )
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
report["result"] = "passed"
destination = artifacts_root() / "stage1/houdini-native-normals-smoke.json"
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
