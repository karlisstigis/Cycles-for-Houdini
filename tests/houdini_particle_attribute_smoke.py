# SPDX-License-Identifier: Apache-2.0
"""Unused simulation attributes cannot hide particles, alter blur or force uploads."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
exec((root / "tests/particle_attribute_fixture.py").read_text())
p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
hou.setFps(24)
hou.setFrame(1)
obj, node = particle_attribute_fixture()
cam = hou.node("/obj").createNode("cam")
cam.setParms(dict(tz=6, projection=1, orthowidth=6))
material = dict(key="surface", kind="emission", color=(0.2, 0.7, 0.1), strength=1.0)
checks = []
assert not ch.capabilities()["particle_lifetime_clipping"]


def pixels(f):
    return np.frombuffer(f["planes"]["combined"]["pixels"], np.float32).copy()


with ch.Session(device=device, width=192, height=96, samples=64, interactive=False) as session:

    def render(mode, motion=False, method="velocity", time=0):
        node.parm("mode").set(mode)
        frame = session.wait(
            session.apply(
                dict(
                    geometry=[ch.geometry_request(obj, node, material="surface", motion=method, at_time=time)],
                    materials=[material],
                    camera_path=cam.path(),
                    background=(0, 0, 0),
                    motion=dict(enabled=motion, fps=24.0, shutter_frames=1.0, samples=3),
                ),
                at_time=time,
            )
        )
        assert session.status()["points"] == (5 if node.evalParm("remove_first") else 6)
        assert not session.status()["source_diagnostics"], session.status()["source_diagnostics"]
        assert np.isfinite(pixels(frame)).all()
        return pixels(frame)

    for enabled, method in ((False, "velocity"), (True, "velocity"), (True, "transform"), (True, "deformation")):
        baseline = render(0, enabled, method)
        before = session.status()["geometry_conversions"]
        for mode in (1, 2, 3, 4, 5, 6, 0):
            assert np.array_equal(render(mode, enabled, method), baseline), (enabled, method, mode)
            if method != "deformation":
                assert session.status()["geometry_conversions"] == before, (method, mode, session.status())
            checks.append(dict(motion=enabled, method=method, attributes=mode, geometry_conversions=before))
    baseline = render(0, True)
    node.parm("speed").set(0)
    stationary = render(2, True)
    assert not np.array_equal(baseline, stationary), "Explicit velocity edits must update blur"
    node.parm("speed").set(12)
    assert np.array_equal(render(2, True), baseline)
    checks.append("Velocity controls blur independently of stopped/dead flags")
    node.parm("remove_first").set(1)
    removed = render(2, True)
    assert not np.array_equal(removed, baseline)
    node.parm("remove_first").set(0)
    assert np.array_equal(render(2, True), baseline)
    checks.append("SOP point deletion controls the rendered population")
    output = artifacts_root() / "stage3/particle-attributes/frames"
    output.mkdir(parents=True, exist_ok=True)
    for frame, mode in enumerate((0, 2, 3, 4, 0), 1):
        hou.setFrame(frame)
        render(mode, True, time=hou.time())
        path = output / ("%s-%d.exr" % ("cpu" if device == "CPU" else "optix", frame))
        session.write_exr(path)
        reader = OIIO.ImageInput.open(str(path))
        assert reader
        assert json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]") == []
        reader.close()
    checks.append("Five frames written without spurious diagnostics")
path = artifacts_root() / "stage3/particle-attributes/houdini-particle-attribute-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(dict(result="passed", device=device, checks=checks), indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
