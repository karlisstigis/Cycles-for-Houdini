# SPDX-License-Identifier: Apache-2.0
"""Graph acquisition precedes conversion and cannot mutate its acquired SOP."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/geometry-ownership/material-acquisition.json"
report = dict(result="running", device=args.device, checks=[])
obj = hou.node("/obj").createNode("geo", "material_acquisition", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
reverse = obj.createNode("reverse")
reverse.setInput(0, grid)
source = obj.createNode("xform")
source.setInput(0, reverse)
camera = hou.node("/obj").createNode("cam")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
builder = ch.create_material(name="acquired_surface", shader="emission")
builder.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
builder.node("surface_shader").parm("strength").set(1)
obj.parm("shop_materialpath").set(builder.path())
try:
    with ch.Session(device=args.device, width=32, height=32, samples=8, passes=["combined", "position"]) as session:
        calls = []

        def recooking_resolver(references, at_time):
            # This callback represents host parameter evaluation causing a
            # dependent SOP recook, not a permitted operation on a render thread.
            before = ch._native.native_stats(session._handle)
            assert before["mesh_topology_builds"] == 0, before
            calls.extend(row["key"] for row in references)
            source.parm("tz").set(1)
            assert abs(source.geometry().point(0).position()[2] - 1) < 1e-6
            return native_scene.resolve_materials(references, at_time)

        update = dict(
            camera_path=camera.path(),
            geometry=[ch.geometry_request(obj, source, material="fallback", material_mode="native")],
            material_resolver=recooking_resolver,
            background=(0, 0, 0),
        )
        frame = session.wait(session.apply(update))
        position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(32, 32, 3)[16, 16]
        assert abs(position[2]) < 1e-5, position
        assert len(calls) == len(set(calls)) == 1, calls
        report["checks"].append(
            "Graph callback runs before mesh conversion; callback recook cannot modify acquired geometry"
        )
        update["material_resolver"] = native_scene.resolve_materials
        frame = session.wait(session.apply(update))
        position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(32, 32, 3)[16, 16]
        assert abs(position[2] - 1) < 1e-5, position
        report["checks"].append("Next acquisition sees the new SOP state")
        bad = ch.create_material(name="unused_invalid_graph", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        bad.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
        bad.node("surface_output").setInput(0, None)
        obj.parm("shop_materialpath").set(bad.path())
        assigned = obj.createNode("material")
        assigned.setInput(0, source)
        assigned.parm("shop_materialpath1").set(builder.path())
        update["geometry"] = [
            ch.geometry_request(obj, assigned, material="fallback", material_mode="native", motion="deformation")
        ]
        update["motion"] = dict(enabled=True, fps=24, shutter_frames=0.5, offset=0, samples=3)
        frame = session.wait(session.apply(update))
        rgb = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(32, 32, 4)[16, 16, :3]
        assert np.allclose(rgb, (0.2, 0.4, 0.8), atol=2e-5), rgb
        report["checks"].append(
            "Invalid graph acquired from unused shutter fallback is deferred and does not reject rendering"
        )
    report["result"] = "passed"
finally:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(destination)
