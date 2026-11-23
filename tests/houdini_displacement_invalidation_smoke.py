# SPDX-License-Identifier: Apache-2.0
"""Surface edits preserve independent displaced geometry, including recovered bindings."""

from support.paths import artifacts_root
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini.queued import WorkerSession

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage3/displacement-invalidation"
destination.mkdir(parents=True, exist_ok=True)
objects = []
for index, x in enumerate((-1, 1)):
    obj = hou.node("/obj").createNode("geo", "binding_" + str(index), run_init_scripts=False)
    obj.parm("tx").set(x)
    geometry = hou.Geometry()
    poly = geometry.createPolygon()
    for xyz in ((-0.75, -0.6, 0), (-0.75, 0.6, 0), (0.75, 0.6, 0), (0.75, -0.6, 0)):
        point = geometry.createPoint()
        point.setPosition(xyz)
        poly.addVertex(point)
    if index == 0:
        geometry.addAttrib(hou.attribType.Point, "Cd", (0.2, 0.6, 0.3))
    stash = obj.createNode("stash")
    stash.parm("stash").set(geometry)
    objects.append((obj, stash))
camera = hou.node("/obj").createNode("cam", "binding_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
ep = lambda name, socket: dict(node=name, socket=socket)
graph = dict(
    nodes=[
        dict(id="attribute", definition="cycles/attribute/1", parameters=dict(attribute="Cd")),
        dict(id="emission", definition="cycles/emission/1", parameters=dict(strength=1.0)),
        dict(
            id="height",
            definition="cycles/displacement/1",
            parameters=dict(height=0.25, midlevel=0.0, scale=1.0, space="object"),
        ),
    ],
    links=[{"from": ep("attribute", "color"), "to": ep("emission", "color")}],
    surface=ep("emission", "emission"),
    displacement=ep("height", "displacement"),
    displacement_method="true",
)
material = lambda: dict(key="surface", kind="graph", graph=deepcopy(graph))
checks = []
with WorkerSession(
    device=device,
    width=128,
    height=64,
    samples=8,
    threads=2,
    passes=["combined", "position"],
    collect_diagnostics=True,
    execution="worker",
) as session:

    def render(label, strength=1.0, height=0.25, changes=False):
        frame = session.wait(
            session.apply(
                dict(materials=[material()])
                if changes
                else dict(
                    materials=[material()],
                    geometry=[ch.geometry_request(obj, sop, material="surface") for obj, sop in objects],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                )
            )
        )
        status = session.status()

        def pixels(name):
            field = frame["planes"][name]
            return np.frombuffer(field["pixels"], np.float32).reshape(64, 128, field["channels"])

        np.testing.assert_allclose(
            pixels("combined")[32, 32, :3], np.array((0.2, 0.6, 0.3)) * strength, atol=1e-5, rtol=0
        )
        np.testing.assert_allclose(pixels("combined")[32, 96, :3], (1, 0, 1), atol=1e-5, rtol=0)
        np.testing.assert_allclose(pixels("position")[32, [32, 96], 2], height, atol=1e-5, rtol=0)
        assert (
            len(status["source_diagnostics"]) == 1
            and status["source_diagnostics"][0]["code"] == "material.missing_color"
        ), status
        checks.append(
            dict(
                label=label,
                displacement_rebuilds=status["displacement_rebuilds"],
                geometry_conversions=status["geometry_conversions"],
            )
        )
        return status

    baseline = render("independent true displacement on valid and recovered surfaces")
    graph["nodes"][1]["parameters"]["strength"] = 2.0
    edited = render("surface strength edit", strength=2.0, changes=True)
    assert edited["displacement_rebuilds"] == baseline["displacement_rebuilds"], (baseline, edited)
    graph["nodes"].reverse()
    reordered = render("source node insertion order", strength=2.0, changes=True)
    assert reordered["displacement_rebuilds"] == baseline["displacement_rebuilds"], (baseline, reordered)
    graph["nodes"][0]["parameters"]["height"] = 0.4
    displaced = render("real displacement input edit", strength=2.0, height=0.4, changes=True)
    assert displaced["displacement_rebuilds"] == baseline["displacement_rebuilds"] + 2, (baseline, displaced)
    assert displaced["geometry_conversions"] == baseline["geometry_conversions"]
(destination / "report.json").write_text(
    json.dumps(dict(result="passed", device=device, checks=checks), indent=2), encoding="utf-8"
)
print(destination / "report.json")
