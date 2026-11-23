# SPDX-License-Identifier: Apache-2.0
"""Size recovery matches sanitized geometry, including packed variants and shutter samples."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
exec((root / "tests/size_recovery_fixture.py").read_text())
p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
hou.setFps(24)
hou.setFrame(1)
cam = hou.node("/obj").createNode("cam")
cam.setParms(dict(tz=6, projection=1, orthowidth=4))
material = dict(key="surface", kind="emission", color=(0.2, 0.7, 0.1), strength=1.0)
checks = []


def pixels(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()


with ch.Session(
    device=device, width=160, height=96, samples=8, interactive=False, passes=["combined", "position"]
) as session:
    for curves in (False, True):
        obj, node = size_fixture(curves)
        pack = obj.createNode("pack")
        pack.setInput(0, node)
        size = "curve_width" if curves else "point_radius"
        code = "geometry.invalid_width" if curves else "geometry.invalid_pscale"

        def render(mode, owner=0, packed=False, second=False, motion=False, default=0.3):
            node.setParms(dict(mode=mode, owner=owner, fallback=default))
            request = ch.geometry_request(
                obj,
                pack if packed else node,
                material="surface",
                motion="deformation" if motion else "transform",
                **{size: default},
            )
            requests = [request]
            if second:
                requests.append(dict(request, object_key="second", **{size: 0.5}))
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=requests,
                        materials=[material],
                        camera_path=cam.path(),
                        background=(0, 0, 0),
                        motion=dict(enabled=motion, fps=24.0, shutter_frames=1.0, samples=3),
                    ),
                    at_time=0,
                ),
                timeout=120,
            )
            assert session.status()["state"] != "failed"
            assert np.isfinite(pixels(frame)).all() and np.count_nonzero(pixels(frame)) > 100
            return frame

        for owner in range(4 if curves else 1):
            baseline = pixels(render(0, owner))
            for mode, reference in ((1, 2), (3, 6), (4, 6), (5, 6)):
                broken = pixels(render(mode, owner))
                issues = session.status()["source_diagnostics"]
                assert len(issues) == 1 and issues[0]["code"] == code, issues
                converted = session.status()["geometry_conversions"]
                session.wait(session.apply(dict(camera_path=cam.path())))
                assert (
                    session.status()["source_diagnostics"] == issues
                    and session.status()["geometry_conversions"] == converted
                )
                expected = pixels(render(reference, owner))
                assert not session.status()["source_diagnostics"]
                assert np.array_equal(broken, expected), (curves, owner, mode, float(np.max(np.abs(broken - expected))))
                assert np.array_equal(pixels(render(0, owner)), baseline)
                assert not session.status()["source_diagnostics"]
                checks.append(dict(curves=curves, owner=owner, mode=mode))
            assert np.array_equal(pixels(render(7, owner)), pixels(render(6, owner)))
            assert not session.status()["source_diagnostics"]
        for mode in (0, 1, 3, 4, 5, 7, 0):
            render(mode, packed=True, second=True)
            assert session.status()["geometry_resources"] == (1 if mode == 0 else 2), session.status()
            assert session.status()["object_resources"] == 2
        checks.append(dict(curves=curves, packed_variants="passed"))
        broken = pixels(render(8, motion=True))
        issues = session.status()["source_diagnostics"]
        assert any(i["code"] == code and "shutter sample" in i["message"] for i in issues), issues
        assert np.array_equal(broken, pixels(render(9, motion=True)))
        assert not session.status()["source_diagnostics"]
        render(0, motion=True)
        assert not session.status()["source_diagnostics"]
        checks.append(dict(curves=curves, shutter_recovery="passed"))
    # Houdini permits conflicting ownership via file/stash data. A valid
    # winning vertex width must ignore a malformed lower-priority stream.
    obj, node = size_fixture(True)
    node.setParms(dict(mode=0, owner=1))
    data = hou.Geometry()
    data.merge(node.geometry())
    data.addAttrib(hou.attribType.Point, "unused_width", "bad")
    geo = artifacts_root() / "stage3/materials/recovery/width-precedence.geo"
    geo.parent.mkdir(parents=True, exist_ok=True)
    data.saveToFile(str(geo))
    geo.write_text(geo.read_text().replace('"unused_width"', '"width"'))
    data = hou.Geometry()
    data.loadFromFile(str(geo))
    assert data.findVertexAttrib("width") and data.findPointAttrib("width")
    stash = obj.createNode("stash")
    stash.parm("stash").set(data)
    request = ch.geometry_request(obj, stash, material="surface", curve_width=0.3)
    scene = dict(
        geometry=[request],
        materials=[material],
        camera_path=cam.path(),
        background=(0, 0, 0),
        motion=dict(enabled=False),
    )
    expected = pixels(session.wait(session.apply(scene)))
    assert not session.status()["source_diagnostics"]
    data.findPointAttrib("width").destroy()
    stash.parm("stash").set(data)
    assert np.array_equal(expected, pixels(session.wait(session.apply(scene))))
    assert not session.status()["source_diagnostics"]
    checks.append("Malformed lower-priority width is ignored")
    # Mixed components must classify only their own consumed offsets. The mesh
    # has invalid pscale, but the loose particles have valid sizes and still share.
    obj, node = size_fixture(False)
    node.parm("mode").set(0)
    box = obj.createNode("box")
    bad = obj.createNode("attribwrangle")
    bad.setInput(0, box)
    bad.parm("snippet").set("f@pscale=-1;")
    merge = obj.createNode("merge")
    merge.setInput(0, node)
    merge.setInput(1, bad)
    pack = obj.createNode("pack")
    pack.setInput(0, merge)
    first = ch.geometry_request(obj, pack, material="surface", point_radius=0.3)
    second = dict(first, object_key="mixed_second", point_radius=0.5)
    scene["geometry"] = [first, second]
    session.wait(session.apply(scene))
    status = session.status()
    assert status["geometry_resources"] == 2 and status["object_resources"] == 4, status
    assert not status["source_diagnostics"], status["source_diagnostics"]
    checks.append("Unused mesh sizes do not split shared point geometry")
report = dict(result="passed", device=device, checks=checks)
path = artifacts_root() / "stage3/materials/recovery/houdini-size-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
