# SPDX-License-Identifier: Apache-2.0
"""Material-only palette changes refresh point/hair shader IDs without geometry edits."""

import argparse
import json
import time
import traceback
import hou
import numpy as np
import cycles_houdini as ch
from parametric_curve_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture()
report = dict(result="running", device=device, events=[])
poly = f.obj.createNode("curve", "polygon_strand", exact_type_name=True)
poly.setParms(dict(type="poly", coords="0,-.6,0 0,.6,0"))
point = f.obj.createNode("stash", "point_sphere")
g = hou.Geometry()
g.createPoint().setPosition((0, 0, 0))
point.parm("stash").set(g)
sources = [f.attributes["rational_nurbs"], poly, point]
camera = hou.node("/obj").createNode("cam", "binding_camera")
camera.setParms(dict(projection=1, orthowidth=8, tz=8))
requests = []
for index, (source, x) in enumerate(zip(sources, (-2.5, 0, 2.5))):
    request = ch.geometry_request(
        f.obj, source, material="shared", material_mode="uniform", curve_width=0.3, point_radius=0.3
    )
    request["object_key"] = "binding-object-" + str(index)
    request["transform"] = list(request["transform"])
    request["transform"][3] += x
    requests.append(request)
good = dict(key="shared", kind="emission", color=(0.2, 0.6, 0.1), emission_strength=1, color_attribute="")
session = ch.Session(device=device, width=240, height=120, samples=1)


def check(label, material, expected, warning=False, geometry=False):
    update = dict(materials=[material])
    if geometry:
        update.update(geometry=requests, camera_path=camera.path(), background=(0, 0, 0))
    frame = session.wait(session.apply(update))
    status = session.status()
    combined = frame["planes"]["combined"]
    image = np.frombuffer(combined["pixels"], np.float32).reshape(120, 240, combined["channels"])
    assert status["curves"] == 2 and status["points"] == 1 and status["geometry_resources"] == 3, status
    measured = []
    for x in (-1.7, 0, 2.5):
        color = image[60, round((x / 8 + 0.5) * 240), :3]
        np.testing.assert_allclose(color, expected, atol=0.001, rtol=0)
        measured.append(color.tolist())
    issues = status["source_diagnostics"]
    assert {d["code"] for d in issues} == ({"material.geometry_binding"} if warning else set()), issues
    if warning:
        assert len({d["resource"] for d in issues}) == 3, issues
    if report["events"]:
        assert status["geometry_conversions"] == report["events"][0]["status"]["geometry_conversions"], status
        assert [n.cookCount() for n in sources] == cooks
    report["events"].append(dict(label=label, status=status, colors=measured))
    return status


try:
    check("Initial emission", good, good["color"], geometry=True)
    cooks = [n.cookCount() for n in sources]
    allocations = []
    for cycle in range(4):
        for kind in ("principled_volume", "volume_absorption"):
            check("Unusable " + kind, dict(good, kind=kind), (1, 0, 1), True)
            repaired = dict(good, color=(0.1, 0.3, 0.9))
            check("Repaired " + kind, repaired, repaired["color"])
        check("Explicit diagnostic", dict(good, kind="diagnostic_surface", color=(1, 0, 1)), (1, 0, 1))
        status = check("Return to emission", good, good["color"])
        allocations.append(status["device_allocation_bytes"])
    assert max(allocations[1:]) - min(allocations[1:]) <= 8192, allocations
    session.close()
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    report.update(
        result="passed",
        warm_allocations=allocations,
        closed=closed,
        scope="CPU/OptiX material-only primitive shader refresh; tracked buffers, not physical-memory certification",
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    session.close()
    (f.dest / "binding-refresh-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], events=len(report["events"]))))
