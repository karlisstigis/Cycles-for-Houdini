# SPDX-License-Identifier: Apache-2.0
"""Actual showcase ground: position pass proves true displacement and animation."""

from support.paths import artifacts_root

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import runpy

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
folder = artifacts_root() / "examples/sop-fx-courtyard"
hou.hipFile.load(str(folder / "sop-fx-courtyard-current.hiplc"), suppress_save_prompt=True)
hou.putenv("OCIO", str(folder / "showcase.ocio"))
hou.Color.reloadOCIO()
ch.synchronize_color_configuration()
metadata = runpy.run_path(str(root / "examples/sop-fx-courtyard/adaptive_scene.py"), run_name="showcase_upgrade")[
    "upgrade"
]()
ground = hou.node(metadata["ground"])
material = hou.node(metadata["material"])
turbine = hou.node(metadata["turbine"])
grid = hou.node("/obj/fx_courtyard/NOISE_DISPLACED_GROUND")
pass_fixture.parm(grid, "resolution").set(128)
rop = hou.node("/out/cycles_sop_courtyard")
camera = hou.node("/obj/courtyard_camera")
# An independent top-down projection makes world Y directly observable.
camera.parmTuple("t").set((0, 10, 0))
camera.parmTuple("r").set((-90, 0, 0))
pass_fixture.parm(camera, "projection").set(1)
pass_fixture.parm(camera, "orthowidth").set(12)
set_rop_devices(rop, args.device)
pass_fixture.set_parameters(rop, dict(vobject="ground", resx=128, resy=96, samples=2, pass_position=1))
source = ground.renderNode().geometry()
assert source.findPointAttrib("uv")
original = np.asarray(source.pointFloatAttribValues("P"), np.float32).copy()
assert np.max(np.abs(original[1::3] + 0.18)) < 1e-5
checks = []
with ch.Session(
    device=args.device, width=128, height=96, samples=2, interactive=False, collect_diagnostics=True
) as session:

    def render(frame, height):
        hou.setFrame(frame)
        pass_fixture.parm(material, "ground_relief").set(height)
        image = session.wait(session.apply(native_scene.capture(rop)), timeout=120)
        plane = image["planes"]["position"]
        positions = np.frombuffer(plane["pixels"], np.float32).reshape(96, 128, plane["channels"])
        # Ignore image borders; every central ray intersects the known plane.
        y = positions[8:-8, 8:-8, 1].copy()
        assert np.isfinite(y).all()
        return y, image, session.status()

    flat, _, base = render(1, 0)
    assert np.max(np.abs(flat + 0.18)) < 0.001, (float(flat.min()), float(flat.max()))
    displaced, image, one = render(1, 1.1)
    span = float(displaced.max() - displaced.min())
    difference = float(np.max(np.abs(displaced - flat)))
    assert span > 0.15 and difference > 0.08, (span, difference)
    assert one["geometry_conversions"] == base["geometry_conversions"]
    checks.append(
        dict(
            name="Ground noise moves renderer vertices and keeps the SOP flat",
            height_range=span,
            maximum_offset=difference,
        )
    )
    animated, _, two = render(5, 1.1)
    animation = float(np.max(np.abs(animated - displaced)))
    assert animation > 0.02, animation
    assert two["geometry_conversions"] == one["geometry_conversions"]
    checks.append(
        dict(name="Animated 4D noise changes displacement without recapturing geometry", maximum_offset=animation)
    )
    assert np.array_equal(original, np.asarray(ground.renderNode().geometry().pointFloatAttribValues("P"), np.float32))
    pass_fixture.parm(rop, "vobject").set("courtyard_turbine")
    update = native_scene.capture(rop)
    assert len(update["geometry"]) == 1 and update["geometry"][0]["subdivision"]["adaptive"]
    assert update["geometry"][0]["subdivision"]["space"] == "pixel"
    session.wait(session.apply(update), timeout=120)
    status = session.status()
    assert status["subdivision_control_faces"] > 0 and status["subdivision_tessellations"] > 0
    checks.append(
        dict(
            name="Independent turbine uses renderer adaptive subdivision",
            control_faces=status["subdivision_control_faces"],
            rendered_triangles=status["subdivision_triangles"],
        )
    )
report = dict(
    result="passed",
    device=args.device,
    checks=checks,
    scene=metadata,
    scope="Independent top-down position checks of the actual showcase material; not a speed or general motion-blur certification",
)
destination = artifacts_root() / "stage2/showcase-adaptive"
destination.mkdir(parents=True, exist_ok=True)
(destination / "houdini-showcase-displacement-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
