# SPDX-License-Identifier: Apache-2.0
"""Pose fast paths retain displacement orientation, world space and pixel dicing."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import time
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/object-poses"
destination.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "pose_variant_subject", run_init_scripts=False)
grid = geo.createNode("grid")
pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((1, 1))
surface = geo.createNode("reverse")
surface.setInput(0, grid)
camera = hou.node("/obj").createNode("cam", "pose_variant_camera")
pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
material = ch.create_material(name="pose_variant_material", shader="emission")
# The default this test was written for (new nodes now take Blender's).
material.node("surface_shader").setParms(dict(strength=10.0))
material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
displacement = material.createNode("cycles::vector_displacement::1", "offset")
# The default this test was written for (new nodes now take Blender's).
displacement.setParms(dict(scale=1.0))
displacement.parmTuple("vector").set((0, 0, 0.3))
material.node("surface_output").setNamedInput("displacement", displacement, "displacement")
pass_fixture.parm(material, "cycles_displacement_method").set("true")
rop = hou.node("/out").createNode("cycles_ipr", "pose_variant_render")
set_rop_devices(rop, device)
configure_render(
    rop,
    dict(
        source_mode="sop",
        soppath=surface.path(),
        camera=camera.path(),
        resx=160,
        resy=90,
        samples=32,
        ipr_samples=32,
        viewer_interactive=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        use_material_assignments=0,
        materialpath=material.path(),
        pass_depth=1,
        pass_position=1,
        pass_normal=1,
    ),
)
checks = []
references = []


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def close(session):
    session.close()
    until = time.monotonic() + 15
    while session.status()["state"] != "closed":
        assert time.monotonic() < until, "Pose variant worker did not retire"
        time.sleep(0.01)


session = ch.Session(device=device, width=160, height=90, samples=32, interactive=True, execution="worker")
try:

    def render(label, z):
        capture = native_scene.capture(rop)
        frame = session.wait(session.apply(capture, at_time=hou.time()))
        mask = plane(frame, "depth")[:, :, 0] < 50
        assert np.count_nonzero(mask) > 100
        actual = float(np.median(plane(frame, "position")[:, :, 2][mask]))
        assert abs(actual - z) < 0.0001, (label, actual, z)
        status = session.status()
        checks.append(
            dict(
                label=label,
                z=actual,
                geometry_conversions=status["geometry_conversions"],
                displacement_rebuilds=status["displacement_rebuilds"],
                tessellations=status["subdivision_tessellations"],
            )
        )
        references.append((capture, frame))
        return status

    for space in ("object", "world"):
        geo.parmTuple("s").set((1, 1, 1))
        pass_fixture.parm(geo, "tx").set(0)
        pass_fixture.parm(displacement, "space").set(space)
        first = render(space + " baseline", 0.3)
        pass_fixture.parm(geo, "tx").set(0.2)
        moved = render(space + " translation", 0.3)
        assert moved["geometry_conversions"] == first["geometry_conversions"]
        if space == "object":
            assert moved["displacement_rebuilds"] == first["displacement_rebuilds"]
        geo.parmTuple("s").set((-1, 0.8, 1))
        render(space + " negative orientation", 0.3)
        geo.parmTuple("s").set((-1.6, 0.6, 2))
        render(space + " nonuniform scale", 0.6 if space == "object" else 0.3)
    # Isolate camera-adaptive subdivision from displacement dependency flags.
    pass_fixture.parm(material, "cycles_displacement_method").set("bump")
    geo.parmTuple("s").set((1, 1, 1))
    configure_render(rop, dict(subdivision=dict(type="linear", space="pixel", dicing_rate=4, max_level=5)))
    base = render("pixel dicing baseline", 0)
    geo.parmTuple("s").set((1.4, 0.8, 1))
    moved = render("pixel dicing after pose", 0)
    assert moved["subdivision_tessellations"] > base["subdivision_tessellations"]
    assert moved["geometry_conversions"] == base["geometry_conversions"]
finally:
    close(session)

# Stored host acquisition requests contain explicit transforms/settings. SOP
# positions stay fixed. Reconvert these native sources with a fresh renderer;
# analytic world-position checks above independently fix the expected effect.
errors = []
with ch.Session(device=device, width=160, height=90, samples=32, interactive=False, execution="sync") as reference:
    for capture, actual in references:
        expected = reference.wait(reference.apply(capture, at_time=hou.time()))
        error = float(np.abs(plane(actual, "combined") - plane(expected, "combined")).mean())
        assert error < 0.002, error
        errors.append(error)
report = dict(result="passed", device=device, checks=checks, fresh_reference_mean_errors=errors)
(destination / "houdini-pose-variants-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
