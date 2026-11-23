# SPDX-License-Identifier: Apache-2.0
"""Native procedural noise: direct kernel values, coordinates and true displacement."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/noise"
destination.mkdir(parents=True, exist_ok=True)
reference_path = destination / "direct-kernel-reference.json"
subprocess.run(
    [str(runtime_executable("ch_harness.exe")), "--noise-reference", "--output", str(reference_path)], check=True
)
reference = json.loads(reference_path.read_text())
assert reference["result"] == "passed" and len(reference["cases"]) == 40
obj = hou.node("/obj").createNode("geo", "noise_receiver", run_init_scripts=False)
grid = obj.createNode("grid", "coarse_surface")
for name, value in dict(type=0, orient=0, rows=2, cols=2).items():
    grid.parm(name).set(value)
grid.parmTuple("size").set((2, 2))
camera = hou.node("/obj").createNode("cam", "noise_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(3)
builder = ch.create_material(name="noise_reference", shader="emission")
emit = builder.node("surface_shader")
emit.parm("strength").set(1)
noise = builder.createNode("cycles::noise_texture::1", "fractal_noise")
assert noise.parm("vector") is None, "Implicit world coordinates must remain a port"
coordinates = builder.createNode("cycles::combine_xyz::1", "chosen_coordinates")
for name, value in zip(("x", "y", "z"), (0.3125, -0.625, 0.125)):
    coordinates.parm(name).set(value)
noise.setNamedInput("vector", coordinates, "vector")
for name, value in dict(w=0.375, scale=2.5, detail=2.75, roughness=0.6, lacunarity=2.0, offset=0.8, gain=1.1).items():
    noise.parm(name).set(value)
grey = builder.createNode("cycles::combine_xyz::1", "scalar_to_rgb")
for name in ("x", "y", "z"):
    grey.setNamedInput(name, noise, "fac")
emit.setNamedInput("color", grey, "vector")
surface = builder.node("surface_output")


def material():
    return dict(key="noise", kind="graph", graph=ch.material_graph(builder))


def plane(frame, name):
    value = frame["planes"][name]
    result = np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])
    assert np.isfinite(result).all()
    return result


def request(key="receiver", x=0, subdivision=None):
    result = ch.geometry_request(obj, grid, material="noise", subdivision=subdivision)
    result["object_key"] = key
    result["transform"][3] = x
    return result


reports = []
for device in ("CPU", ch.default_device()):
    checks = []
    noise.setNamedInput("vector", coordinates, "vector")
    emit.setNamedInput("color", grey, "vector")
    surface.setInput(surface.inputIndex("displacement"), None)
    builder.parm("cycles_displacement_method").set("bump")
    with ch.Session(device=device, width=64, height=64, samples=8, collect_diagnostics=True) as session:
        for index, case in enumerate(reference["cases"]):
            noise.parm("dimensions").set(case["dimensions"])
            noise.parm("type").set(case["type"])
            noise.parm("use_normalize").set(case["normalize"])
            update = dict(materials=[material()])
            if index == 0:
                update.update(geometry=[request()], camera_path=camera.path(), background=(0, 0, 0))
            actual = plane(session.wait(session.apply(update)), "combined")[32, 32, :3]
            np.testing.assert_allclose(actual, case["fac"], rtol=2e-5, atol=2e-5, err_msg=str(case))
            checks.append(dict(name="Direct kernel scalar reference", case=case, actual=actual.tolist()))
        noise.parm("dimensions").set("3D")
        noise.parm("type").set("fBM")
        noise.parm("use_normalize").set(True)
        emit.setNamedInput("color", noise, "color")
        frame = session.wait(session.apply(dict(materials=[material()])))
        color = plane(frame, "combined")[32, 32, :3]
        expected = next(
            case["fac"]
            for case in reference["cases"]
            if case["dimensions"] == "3D" and case["type"] == "fBM" and case["normalize"]
        )
        np.testing.assert_allclose(color[0], expected, rtol=2e-5, atol=2e-5)
        assert np.ptp(color) > 0.01, color
        checks.append(dict(name="Color output preserves scalar red and independent channels", color=color.tolist()))
        emit.setNamedInput("color", grey, "vector")
        noise.setInput(noise.inputIndex("vector"), None)
        implicit = session.wait(session.apply(dict(materials=[material()])))
        geometry = builder.createNode("cycles::geometry::1", "world_position")
        noise.setNamedInput("vector", geometry, "position")
        explicit = session.wait(session.apply(dict(materials=[material()])))
        np.testing.assert_allclose(plane(implicit, "combined"), plane(explicit, "combined"), rtol=0, atol=1e-6)
        assert np.std(plane(explicit, "combined")[12:52, 12:52, 0]) > 0.025
        checks.append(dict(name="Implicit world coordinates match explicit Geometry Position and vary spatially"))
        noise.setInput(noise.inputIndex("vector"), None)
        displacement = builder.createNode("cycles::displacement::1", "noise_displacement")
        displacement.parm("space").set("object")
        displacement.parm("scale").set(0.5)
        displacement.setNamedInput("height", noise, "fac")
        surface.setNamedInput("displacement", displacement, "displacement")
        builder.parm("cycles_displacement_method").set("true")
        emit.setInput(emit.inputIndex("color"), None)
        emit.parmTuple("color").set((0.2, 0.4, 0.8))
        subdivision = dict(
            type="linear",
            space="object",
            dicing_rate=0.04,
            max_level=6,
            boundary="edge_and_corner",
            fvar="boundaries",
            triangle_limit=100000,
        )
        frame = session.wait(session.apply(dict(materials=[material()], geometry=[request(subdivision=subdivision)])))
        position = plane(frame, "position")
        covered = plane(frame, "combined")[:, :, 3] > 0.999
        assert np.ptp(position[:, :, 2][covered]) > 0.06, "True displacement must alter geometry"
        assert max(abs(point.position()[2]) for point in grid.geometry().points()) == 0
        status = session.status()
        assert status["subdivision_triangles"] > 2, status
        checks.append(
            dict(
                name="Native noise changes subdivided mesh positions and keeps coarse SOP data",
                status=status,
                z_range=[float(position[:, :, 2][covered].min()), float(position[:, :, 2][covered].max())],
            )
        )
        original = plane(frame, "position").copy()
        conversions = session.status()["geometry_conversions"]
        noise.parm("scale").set(4.25)
        edited = plane(session.wait(session.apply(dict(materials=[material()]))), "position")
        assert np.max(np.abs(edited[:, :, 2] - original[:, :, 2])) > 0.02
        noise.parm("scale").set(2.5)
        restored = plane(session.wait(session.apply(dict(materials=[material()]))), "position")
        np.testing.assert_allclose(restored, original, rtol=0, atol=1e-5)
        assert session.status()["geometry_conversions"] == conversions
        checks.append(dict(name="Noise edits rebuild displacement, restore originals and retain extraction"))
        session.wait(
            session.apply(dict(geometry=[request("left", -0.55, subdivision), request("right", 0.55, subdivision)]))
        )
        assert session.status()["mesh_variant_resources"] == 2, session.status()
        checks.append(
            dict(name="World-space noise creates distinct instance displacement variants", status=session.status())
        )
        frame = session.wait(session.apply(dict(geometry=[request(subdivision=subdivision)])))
        session.write_exr(
            destination / ("cpu-noise-displacement.exr" if device == "CPU" else "optix-noise-displacement.exr")
        )
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    reports.append(dict(device=device, checks=checks))
report = dict(
    result="passed",
    devices=reports,
    scope="Native noise against direct engine evaluation; coordinate defaults, true displacement, edits and instance variants",
)
(destination / "houdini-noise-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=[len(item["checks"]) for item in reports])))
