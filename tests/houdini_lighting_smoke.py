# SPDX-License-Identifier: Apache-2.0
"""Direct numeric lights with an independent Lambertian irradiance check."""

from support.paths import artifacts_root
from array import array
import json
import math
import os
from pathlib import Path

import hou
import cycles_houdini as ch

root = artifacts_root() / "stage2/lighting"
root.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "direct_light_receiver", run_init_scripts=False)
source = obj.createNode("python", "receiver")
source.parm("python").set("""import hou
g=hou.pwd().geometry(); face=g.createPolygon()
for position in ((-4.,-4.,0.),(4.,-4.,0.),(4.,4.,0.),(-4.,4.,0.)):
    p=g.createPoint(); p.setPosition(position); face.addVertex(p)
""")
camera = hou.node("/obj").createNode("cam", "lighting_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
transform = (1, 0, 0, -4, 0, 1, 0, 0, 0, 0, 1, 5)


def centre(frame):
    data = array("f")
    data.frombytes(frame["planes"]["combined"]["pixels"])
    assert all(math.isfinite(value) for value in data)
    return [
        sum(data[(y * frame["width"] + x) * 4 + c] for y in range(30, 34) for x in range(46, 50)) / 16 for c in range(3)
    ]


reports = []
for device in ("CPU", ch.default_device()):
    checks = []
    with ch.Session(device=device, width=96, height=64, samples=2048) as session:
        scene = dict(
            geometry=[ch.geometry_request(obj, source, material="receiver")],
            materials=[dict(key="receiver", kind="diffuse", color=(1, 1, 1), roughness=0)],
            camera_path=camera.path(),
            background=(0, 0, 0),
        )
        frame = session.wait(session.apply(scene))
        assert max(centre(frame)) == 0
        checks.append(dict(name="Black environment without lights is unlit"))
        for kind in ("point", "area", "sun"):
            light = dict(key="key", kind=kind, transform=transform, radius=0, strength=(100, 100, 100), size=(1, 1))
            frame = session.wait(session.apply(dict(lights=[light])))
            first = centre(frame)
            assert min(first) > 1e-5, (kind, first)
            if kind == "point":
                # I=power/(4*pi); outgoing Lambertian L=I*cos/(distance^2*pi).
                expected = 100 * 5 / (4 * math.pi**2 * 41**1.5)
                assert max(abs(value - expected) for value in first) < 0.001, (device, first, expected)
                checks.append(
                    dict(name="Independent point-light irradiance", measured=first, expected=expected, tolerance=0.001)
                )
            else:
                checks.append(dict(name=f"{kind} direct light contributes", measured=first))
            converted = session.status()["geometry_conversions"]
            light["strength"] = (200, 200, 200)
            frame = session.wait(session.apply(dict(lights=[light])))
            doubled = centre(frame)
            assert max(abs(doubled[i] / first[i] - 2) for i in range(3)) < 0.01, (kind, first, doubled)
            assert session.status()["geometry_conversions"] == converted
            checks.append(
                dict(name=f"{kind} strength edit doubles radiance and retains geometry", before=first, after=doubled)
            )
            light["strength"] = (0, 0, 0)
            frame = session.wait(session.apply(dict(lights=[light])))
            assert max(centre(frame)) == 0
            checks.append(dict(name=f"{kind} zero strength disables contribution"))
            light["strength"] = (100, 0, 0)
            frame = session.wait(session.apply(dict(lights=[light])))
            red = centre(frame)
            assert red[0] > 1e-5 and red[1] == red[2] == 0, (kind, red)
            checks.append(dict(name=f"{kind} RGB strength is preserved", measured=red))
        frame = session.wait(session.apply(dict(deleted_lights=["key"])))
        assert max(centre(frame)) == 0
        assert session.status()["light_resources"] == 0
        checks.append(dict(name="Light deletion restores the black environment"))
        final = session.status()
    assert session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks, final=final))
report = dict(
    result="passed",
    devices=reports,
    scope="Direct point, area and sun numeric strength; point irradiance, linear edits, color, deletion and tracked retirement",
)
(root / "houdini-lighting-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=[len(r["checks"]) for r in reports])))
