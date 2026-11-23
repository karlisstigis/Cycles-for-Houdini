# SPDX-License-Identifier: Apache-2.0
"""Cross-kind shader reuse, graph ownership and bounded retained allocations."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import math
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/retirement"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "retirement_sources", run_init_scripts=False)
mesh = obj.createNode("python", "colored_quad")
mesh.parm("python").set("""import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'Cd',(.25,.5,.75));polygon=g.createPolygon()
for x,y in [(-3,-3),(3,-3),(3,3),(-3,3)]:
    p=g.createPoint();p.setPosition((x,y,0));p.setAttribValue('Cd',(.25,.5,.75));polygon.addVertex(p)
""")
points = obj.createNode("python", "sphere")
points.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'pscale',.3);g.createPoint().setPosition((0,0,0))"
)
hair = obj.createNode("python", "hair")
hair.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'width',.4);c=g.createPolygon(is_closed=False)\nfor y in (-.7,.7):\n    p=g.createPoint();p.setPosition((0,y,0));c.addVertex(p)"
)
path = destination / "owned-retirement-density.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-volume-fixture", str(path)], check=True)
file = obj.createNode("file")
file.parm("file").set(str(path))
volume = obj.createNode("stash", "owned_density")
volume.parm("stash").set(file.geometry())
file.destroy()
path.unlink()
assert volume.geometry().prims()[0].activeVoxelCount() == 729
camera = hou.node("/obj").createNode("cam", "retirement_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)


def graph(color, attribute=False):
    nodes = [dict(id="emission", definition="cycles/emission/1", parameters=dict(color=color, strength=1.0))]
    links = []
    if attribute:
        nodes.append(dict(id="attribute", definition="cycles/attribute/1", parameters=dict(attribute="Cd")))
        links.append(
            dict(**{"from": dict(node="attribute", socket="color"), "to": dict(node="emission", socket="color")})
        )
    return dict(schema_version=1, nodes=nodes, links=links, surface=dict(node="emission", socket="emission"))


def center(frame):
    data = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(
        frame["height"], frame["width"], 4
    )
    return data[
        frame["height"] // 2 - 1 : frame["height"] // 2 + 2, frame["width"] // 2 - 1 : frame["width"] // 2 + 2, :3
    ].mean(axis=(0, 1))


reports = []
for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=64, height=64, samples=512) as session:
        # Worker creation is lazy. Establish a real empty scene before reading
        # the engine's default shader count; the pre-launch status has none.
        session.wait(session.apply(dict(geometry=[], camera_path=camera.path(), background=(0, 0, 0))))
        defaults = session.status()["engine_shader_resources"]
        rows = []
        phases = (
            ("mesh-attribute", mesh, dict(kind="graph", graph=graph((1, 1, 1), True)), (0.25, 0.5, 0.75), (0, 0, 0)),
            (
                "volume",
                volume,
                dict(kind="volume_absorption", color=(0, 0, 0), density=1.0),
                (math.exp(-9 / 8),) * 3,
                (1, 1, 1),
            ),
            ("sphere", points, dict(kind="graph", graph=graph((0.8, 0.1, 0.05))), (0.8, 0.1, 0.05), (0, 0, 0)),
            ("hair", hair, dict(kind="graph", graph=graph((0.05, 0.1, 0.8))), (0.05, 0.1, 0.8), (0, 0, 0)),
        )
        for cycle in range(8):
            for phase, source, definition, expected, background in phases:
                key = f"retired-material:{cycle}:{phase}"
                request = ch.geometry_request(obj, source, material=key)
                frame = session.wait(
                    session.apply(
                        dict(
                            geometry=[request],
                            camera_path=camera.path(),
                            background=background,
                            materials=[dict(definition, key=key)],
                        )
                    )
                )
                actual = center(frame)
                tolerance = 0.02 if phase == "volume" else 0.001
                assert np.allclose(actual, expected, atol=tolerance, rtol=0), (device, cycle, phase, actual, expected)
                status = session.status()
                assert status["material_resources"] == 1 and status["retired_shader_resources"] == 0
                assert status["engine_shader_resources"] == defaults + 1, status
                rows.append(
                    dict(
                        cycle=cycle,
                        phase=phase,
                        expected=list(expected),
                        measured=actual.tolist(),
                        allocation_bytes=status["device_allocation_bytes"],
                        status=status,
                    )
                )
                session.wait(session.apply(dict(geometry=[], deleted_materials=[key])))
                retired = session.status()
                assert retired["material_resources"] == 0 and retired["retired_shader_resources"] == 1
                assert retired["engine_shader_resources"] == defaults + 1, retired
        for phase, *unused in phases:
            allocations = {row["allocation_bytes"] for row in rows if row["cycle"] >= 3 and row["phase"] == phase}
            assert len(allocations) == 1, (
                "Warm allocations grew across reused shader kinds",
                device,
                phase,
                allocations,
            )
        assert session.status()["shader_reuses"] == 31
        reports.append(
            dict(
                device=device, result="passed", default_shader_resources=defaults, renders=rows, final=session.status()
            )
        )
    closed = session.status()
    assert (
        closed["device_allocation_bytes"]
        == closed["engine_shader_resources"]
        == closed["retired_shader_resources"]
        == 0
    )

report = dict(
    result="passed",
    devices=reports,
    scope="32 shader lifetimes per device across Cd graphs, absorption volumes, sphere and hair graphs. A single retained engine shader slot is reused; tracked allocations are stable per warm phase. Physical residency and peak-scene reclamation remain separate work.",
)
(destination / "houdini-shader-retirement-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "houdini-shader-retirement-smoke.json")
