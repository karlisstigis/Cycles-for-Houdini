# SPDX-License-Identifier: Apache-2.0
"""Artist-default Pig Head assignments, local fallback, and live repair."""

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
device = parser.parse_args().device
dest = artifacts_root() / "stage3/materials/recovery"
dest.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "recovery_pig", run_init_scripts=False)
pig = obj.createNode("testgeometry_pighead")
assert pig.parm("addshader").eval() == 1
camera = hou.node("/obj").createNode("cam", "recovery_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
checks = []


def scene(sop=pig):
    return dict(
        geometry=[ch.geometry_request(obj, sop, material_mode="native", material="default")],
        materials=[dict(key="default", kind="diffuse", color=(0.3, 0.3, 0.3))],
        material_resolver=native_scene.resolve_materials,
        camera_path=camera.path(),
        background=(0, 0, 0),
        cryptomatte=dict(object=True, material=True, asset=False, levels=4),
    )


def rgb(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(frame["height"], frame["width"], 4)[
        ..., :3
    ]


with ch.Session(device=device, width=160, height=120, samples=4, interactive=False) as session:
    frame = session.wait(session.apply(scene()), timeout=120)
    pixels = rgb(frame)
    marker = (pixels[..., 0] > 0.8) & (pixels[..., 1] < 0.1) & (pixels[..., 2] > 0.8)
    assert marker.sum() > 1000, "Default pig is not visibly marked in an unlit scene"
    status = session.status()
    issues = status["source_diagnostics"]
    assert len(issues) == 3 and all(d["code"] == "material.unsupported" for d in issues), issues
    assert len({d["resource"] for d in issues}) == 3
    metadata = session.output_metadata()
    for key, value in metadata.items():
        if key.endswith("/name") and value == "CryptoMaterial":
            manifest = json.loads(metadata[key[:-4] + "manifest"])
            assert {d["source"] for d in issues}.issubset(manifest), (manifest, issues)
            assert len({manifest[d["source"]] for d in issues}) == 3
            break
    else:
        raise AssertionError("No material Cryptomatte manifest")
    assert not status["error"] and status["state"] == "complete"
    session.write_exr(dest / "stock-pig.exr")
    checks.append("Stock pig with default Principled assignments renders visibly; three distinct diagnostics")

    # Camera-only edits keep diagnostics and geometry intact.
    conversions = status["geometry_conversions"]
    camera.parm("tx").set(0.15)
    session.wait(session.apply(dict(camera_path=camera.path())))
    assert session.status()["source_diagnostics"] == issues
    assert session.status()["geometry_conversions"] == conversions
    checks.append("Camera edits retain warnings without geometry conversion")

    small = obj.createNode("xform")
    small.setInput(0, pig)
    small.parm("scale").set(0.35)
    targets = obj.createNode("grid")
    targets.setParms(dict(rows=2, cols=2, orient=0))
    targets.parmTuple("size").set((2, 1.4))
    copies = obj.createNode("copytopoints")
    copies.setInput(0, small)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    frame = session.wait(session.apply(scene(copies)))
    status = session.status()
    assert status["object_resources"] == 4 and status["geometry_resources"] == 1, status
    assert len(status["source_diagnostics"]) == 3, status
    assert ((rgb(frame)[..., 0] > 0.8) & (rgb(frame)[..., 2] > 0.8)).sum() > 500
    checks.append("Packed stock pigs retain one shared prototype and three source identities without user IDs")

    # Native Material SOP replaces assignments using ordinary Houdini controls.
    assignment = obj.createNode("material", "artist_assignment")
    assignment.setInput(0, pig)
    valid = ch.create_material(name="recovery_valid", shader="emission")
    valid.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
    valid.node("surface_shader").parm("strength").set(1)
    assignment.parm("shop_materialpath1").set(valid.path())
    frame = session.wait(session.apply(scene(assignment)))
    assert not session.status()["source_diagnostics"], session.status()
    assert ((rgb(frame)[..., 1] > 0.6) & (rgb(frame)[..., 0] < 0.2)).sum() > 1000
    checks.append("Assigning a supported material restores shading and clears diagnostics")

    # Disconnected invalid VOPs must not poison the active surface.
    unused = valid.createNode("cycles::noise_texture::1", "unused_bypassed_vop")
    # The default this test was written for (new nodes now take Blender's).
    unused.setParms(dict(scale=1.0))
    unused.bypass(True)
    frame = session.wait(session.apply(scene(assignment)))
    assert not session.status()["source_diagnostics"]
    checks.append("Disconnected bypassed VOP is ignored")

    # An invalid reachable graph is a local source issue, not an engine failure.
    valid.node("surface_output").setInput(0, None)
    frame = session.wait(session.apply(scene(assignment)))
    assert session.status()["source_diagnostics"][0]["code"] == "material.invalid_graph"
    assert ((rgb(frame)[..., 0] > 0.8) & (rgb(frame)[..., 2] > 0.8)).sum() > 1000
    valid.node("surface_output").setInput(0, valid.node("surface_shader"))
    session.wait(session.apply(scene(assignment)))
    assert not session.status()["source_diagnostics"]
    checks.append("Broken graph becomes a marker and repairs without restarting the session")

    # Preserve malformed transport/resolver contracts as real failures.
    broken = scene(assignment)
    broken["material_resolver"] = lambda refs, time: []
    try:
        session.wait(session.apply(broken))
    except RuntimeError as error:
        assert "omitted" in str(error), str(error)
    else:
        raise AssertionError("Invalid resolver contract was silently converted into a source warning")
    checks.append("Invalid resolver contract still fails explicitly")

report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-material-recovery-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
