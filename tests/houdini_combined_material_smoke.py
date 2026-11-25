# SPDX-License-Identifier: Apache-2.0
"""Closed-mesh surface/medium output: independent extinction, reuse and packing."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/combined"
dest.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "closed_medium", run_init_scripts=False)
box = obj.createNode("box")
box.parmTuple("size").set((2, 2, 1))
box.setRenderFlag(True)
cam = hou.node("/obj").createNode("cam", "medium_camera")
cam.setParms(dict(tz=5, projection=1, orthowidth=4))
material = hou.node("/mat").createNode("cycles::material::1", "surface_and_medium")
material.node("surface_shader").destroy()
surface = material.createNode("cycles::transparent_bsdf::1", "surface_shader")
medium = material.createNode("cycles::absorption_volume::1", "interior_medium")
medium.parmTuple("color").set((0, 0, 0))
medium.parm("density").set(1)
output = material.node("surface_output")
surface_socket = next(name for name, type in zip(surface.outputNames(), surface.outputDataTypes()) if type == "bsdf")
output.setNamedInput("surface", surface, surface_socket)
output.setNamedInput("volume", medium, "volume")
obj.parm("shop_materialpath").set(material.path())
assert output.inputNames() == ("surface", "displacement", "volume"), output.inputNames()
graph = ch.material_graph(material)
assert "surface" in graph and "volume" in graph
assert ch.capabilities()["graph_combined_outputs"] and ch.capabilities()["closed_mesh_volumes"]
checks = []


def scene(sop=box):
    return dict(
        geometry=[ch.geometry_request(obj, sop, material_mode="native", material="unused")],
        material_resolver=native_scene.resolve_materials,
        camera_path=cam.path(),
        background=(1, 1, 1),
    )


def expect(frame, expected, location=(40, 24)):
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(
        frame["height"], frame["width"], 4
    )
    x, y = location
    actual = pixels[y - 1 : y + 2, x - 1 : x + 2, :3].mean(axis=(0, 1))
    assert np.isfinite(actual).all() and np.max(np.abs(actual - np.asarray(expected))) < 0.025, (
        actual.tolist(),
        expected,
    )
    return actual.tolist()


with ch.Session(
    device=device, width=80, height=48, samples=256, interactive=False, passes=["combined", "position"]
) as session:
    frame = session.wait(session.apply(scene()), timeout=60)
    measured = expect(frame, [math.exp(-1)] * 3)
    initial = session.status()
    session.write_exr(dest / ("combined-" + ("cpu" if device == "CPU" else "optix") + ".exr"))
    checks.append(dict(name="Independent Beer-Lambert extinction through one-unit closed shell", rgb=measured))
    medium.parm("density").set(2)
    expect(session.wait(session.apply(scene()), timeout=60), [math.exp(-2)] * 3)
    assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
    surface.parmTuple("color").set((0.8, 0.6, 1))
    expected = [value * value * math.exp(-2) for value in (0.8, 0.6, 1)]
    expect(session.wait(session.apply(scene()), timeout=60), expected)
    assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
    checks.append(
        dict(
            name="Surface transmission at both boundaries and volume attenuation both contribute; shader edits retain geometry"
        )
    )
    obj.parm("sz").set(2)
    expect(session.wait(session.apply(scene()), timeout=60), [value * value * math.exp(-4) for value in (0.8, 0.6, 1)])
    assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
    obj.parm("sz").set(1)
    output.setInput(2, None)
    expect(session.wait(session.apply(scene()), timeout=60), [value * value for value in (0.8, 0.6, 1)])
    output.setNamedInput("volume", medium, "volume")
    expect(session.wait(session.apply(scene()), timeout=60), expected)
    assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
    checks.append(
        dict(name="Object scale changes physical path length; removing/restoring medium updates retained mesh")
    )
    output.setInput(0, None)
    assert "surface" not in ch.material_graph(material)
    expect(session.wait(session.apply(scene()), timeout=60), [math.exp(-2)] * 3)
    assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
    output.setNamedInput("surface", surface, surface_socket)
    checks.append(dict(name="Volume-only closed shell renders with no Surface connection"))
    # A genuine glass boundary and a translated closed shell test displacement
    # without opening seams or changing the independently known path length.
    glass = material.createNode("cycles::glass_bsdf::1", "glass_boundary")
    # The default this test was written for (new nodes now take Blender's).
    glass.setParms(dict(distribution="ggx"))
    glass.setParms(dict(IOR=1.0, roughness=0.0))
    glass.parmTuple("color").set((1, 1, 1))
    output.setNamedInput("surface", glass, glass.outputNames()[0])
    offset = material.createNode("cycles::combine_xyz::1", "whole_shell_offset")
    offset.parm("z").set(0.2)
    output.setNamedInput("displacement", offset, offset.outputNames()[0])
    material.parm("cycles_displacement_method").set("true")
    displaced = session.wait(session.apply(scene()), timeout=60)
    expect(displaced, [math.exp(-2)] * 3)

    def front_z(frame):
        return float(np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(48, 80, 3)[24, 40, 2])

    assert abs(front_z(displaced) - 0.7) < 0.005, front_z(displaced)
    rebuilds = session.status()["displacement_rebuilds"]
    medium.parm("density").set(3)
    changed = session.wait(session.apply(scene()), timeout=60)
    expect(changed, [math.exp(-3)] * 3)
    assert abs(front_z(changed) - 0.7) < 0.005 and session.status()["displacement_rebuilds"] == rebuilds
    glass.parmTuple("color").set((0.5, 0.5, 0.5))
    expect(session.wait(session.apply(scene()), timeout=60), [0.25 * math.exp(-3)] * 3)
    assert session.status()["displacement_rebuilds"] == rebuilds
    offset.parm("z").set(0.4)
    moved = session.wait(session.apply(scene()), timeout=60)
    assert abs(front_z(moved) - 0.9) < 0.005 and session.status()["displacement_rebuilds"] > rebuilds
    checks.append(
        dict(
            name="Glass, medium and true displacement combine; unrelated surface/volume edits retain displaced geometry"
        )
    )
    output.setInput(1, None)
    material.parm("cycles_displacement_method").set("bump")
    medium.parm("density").set(2)
    output.setNamedInput("surface", surface, surface_socket)
    # Shared coordinate node: UV feeds the surface; Object feeds a medium
    # density expression. Only the surface branch may request a UV attribute.
    uv = obj.createNode("attribwrangle", "constant_surface_uv")
    uv.setInput(0, box)
    uv.parm("snippet").set("v@uv=set(.8,.6,1);")
    coordinate = material.createNode("cycles::texture_coordinate::1", "shared_coordinates")
    uv_color = material.createNode("cycles::vector_math::1", "uv_surface_color")
    uv_color.parm("math_type").set("add")
    uv_color.parmTuple("vector2").set((0, 0, 1))
    uv_color.setNamedInput("vector1", coordinate, "UV")
    surface.setNamedInput("color", uv_color, "vector")
    dot = material.createNode("cycles::vector_math::1", "constant_density_coordinates")
    dot.parm("math_type").set("dot_product")
    dot.parmTuple("vector2").set((0, 0, 0))
    dot.setNamedInput("vector1", coordinate, "object")
    add = material.createNode("cycles::math::1", "density_from_coordinates")
    add.setParms(dict(math_type="add", value2=2))
    add.setNamedInput("value1", dot, "value")
    medium.setNamedInput("density", add, "value")
    expect(session.wait(session.apply(scene(uv)), timeout=60), expected)
    checks.append(
        dict(name="Surface UV and medium Object coordinates share one node with independent output requirements")
    )
    # Four instances share one native closed-mesh prototype and one material.
    points = hou.Geometry()
    for x in (-1.5, 1.5):
        for y in (-1.2, 1.2):
            points.createPoint().setPosition((x, y, 0))
    targets = obj.createNode("stash", "instance_positions")
    targets.parm("stash").set(points)
    copies = obj.createNode("copytopoints", "packed_media")
    copies.setInput(0, uv)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    cam.parm("orthowidth").set(6)
    packed = session.wait(session.apply(scene(copies)), timeout=60)
    for location in ((20, 11), (60, 11), (20, 37), (60, 37)):
        expect(packed, expected, location)
    status = session.status()
    assert status["object_resources"] == 4 and status["geometry_resources"] == 1, status
    checks.append(
        dict(
            name="Four packed media render correctly with one shared prototype",
            objects=status["object_resources"],
            geometry=status["geometry_resources"],
        )
    )
    # Save a clean artist example: density and tint can be edited directly.
    medium.setInput(medium.inputIndex("density"), None)
    surface.setInput(surface.inputIndex("color"), None)
    surface.parmTuple("color").set((0.8, 0.6, 1))
    for unused in (glass, offset, coordinate, uv_color, dot, add):
        unused.destroy()
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    rop = hou.node("/out").createNode("cycles_ipr", "combined_material_render")
    set_rop_devices(rop, device)
    rop.setParms(
        dict(
            vobject=obj.name(),
            camera=cam.path(),
            resx=960,
            resy=720,
            ipr_samples=64,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            world_source="color",
        )
    )
    rop.parmTuple("background").set((1, 1, 1))
    rop.parm("picture").set(str(dest / "surface-volume.$F4.exr"))
    material.layoutChildren()
    obj.layoutChildren()
    saved = ch.material_graph(material)
    hou.hipFile.save(str(dest / "surface-volume-example.hiplc"))
assert session.status()["device_allocation_bytes"] == 0

# The same typed graph validator checks each output domain independently.
with ch.Session(device=device, width=80, height=48, samples=1, interactive=False, execution="sync") as validator:
    accepted = validator.wait(validator.apply(scene(copies)))
    previous = validator.status()
    previous_frame = validator.frame_info()
    candidate = deepcopy(saved)
    candidate["volume"] = candidate["surface"]

    def replacement(graph):
        return dict(
            material_resolver=lambda references, now: [
                dict(key=reference["key"], kind="graph", graph=graph) for reference in references
            ]
        )

    validator.wait(validator.apply(replacement(candidate)))
    assert any(d["code"] == "material.volume_source_unavailable" for d in validator.status()["source_diagnostics"])
    candidate = deepcopy(saved)
    candidate["surface"] = candidate["volume"]
    validator.wait(validator.apply(replacement(candidate)))
    assert any(d["code"] == "material.shader_domain" for d in validator.status()["source_diagnostics"])
    validator.wait(validator.apply(replacement(saved)))
    assert not validator.status()["source_diagnostics"]
    checks.append(dict(name="Cross-domain miswiring recovers locally and repairs clear warnings"))

hou.hipFile.load(str(dest / "surface-volume-example.hiplc"), suppress_save_prompt=True)
assert ch.material_graph(hou.node("/mat/surface_and_medium")) == saved
checks.append(dict(name="Native material output and both connections survive HIP reload"))

# The SOP bounds and the OBJ transform deliberately have different origins.
# Compare Generated to an explicit normalization of the raw Object coordinate.
fog = hou.node("/obj").createNode("geo", "generated_fog", run_init_scripts=False)
fog_box = fog.createNode("box")
fog_box.parmTuple("t").set((0, 2.5, 0))
fog_box.parmTuple("size").set((2.8, 1.5, 0.8))
fog.parm("ty").set(0.6)
fog_cam = hou.node("/obj").createNode("cam", "generated_fog_camera")
fog_cam.setParms(dict(ty=3.1, tz=5, projection=1, orthowidth=4))
profile = ch.create_material(name="generated_fog_profile", shader="emission")
fog.parm("shop_materialpath").set(profile.path())
profile_output = profile.node("surface_output")
glow = profile.node("surface_shader")
glow.parmTuple("color").set((1, 1, 1))
coords = profile.createNode("cycles::texture_coordinate::1", "fog_coordinates")
split = profile.createNode("cycles::separate_xyz::1", "fog_height")
split.setNamedInput("vector", coords, "generated")
reverse = profile.createNode("cycles::map_range::1", "reverse_height")
reverse.setParms(dict(from_min=0, from_max=1, to_min=1, to_max=0, clamp=1))
reverse.setNamedInput("value", split, "y")
curve = profile.createNode("cycles::float_curve::1", "fog_density_profile")
curve.parmTuple("curve_domain").set((0, 1))
curve.parm("fac").set(1)
curve.parm("curve_extension").set(1)
curve.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 3, (0, 0.5, 1), (0.15, 0.45, 1.3)))
curve.setNamedInput("value", reverse, "result")
glow.setNamedInput("strength", curve, "value")
absorption = profile.createNode("cycles::absorption_volume::1", "fog_absorption")
absorption.parmTuple("color").set((0, 0, 0))
absorption.setNamedInput("density", curve, "value")
fog_scene = dict(
    geometry=[ch.geometry_request(fog, fog_box, material_mode="native", material="unused")],
    material_resolver=native_scene.resolve_materials,
    camera_path=fog_cam.path(),
    background=(1, 1, 1),
)
# Three central bands cover both segments of the piecewise linear curve.
rows = [14, 23, 33]


def fog_bands(frame, plane_name, channel):
    plane = frame["planes"][plane_name]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(48, 80, plane["channels"])
    return pixels[rows, 36:44, channel].mean(axis=1)


with ch.Session(
    device=device, width=80, height=48, samples=256, interactive=False, passes=["combined", "position"]
) as fog_session:

    def render_fog():
        frame = fog_session.wait(fog_session.apply(fog_scene), timeout=60)
        assert not fog_session.status()["source_diagnostics"], fog_session.status()
        return frame

    # Derive the analytic control from the surface hit positions, in world units.
    surface_frame = render_fog()
    world_y = fog_bands(surface_frame, "position", 1)
    assert np.ptp(world_y) > 0.8 and np.all((world_y > 2.35) & (world_y < 3.85)), world_y
    expected_density = np.interp((3.85 - world_y) / 1.5, (0, 0.5, 1), (0.15, 0.45, 1.3))
    surface_density = fog_bands(surface_frame, "combined", 0)
    np.testing.assert_allclose(surface_density, expected_density, rtol=0, atol=0.008)

    profile_output.setInput(profile_output.inputIndex("surface"), None)
    profile_output.setNamedInput("volume", absorption, "volume")
    split.setNamedInput("vector", coords, "object")
    reverse.setParms(dict(from_min=1.75, from_max=3.25))
    explicit = fog_bands(render_fog(), "combined", 0)
    fog_session.write_exr(dest / "fog-explicit-object-bounds.exr")
    expected_transmission = np.exp(-0.8 * expected_density)
    np.testing.assert_allclose(explicit, expected_transmission, rtol=0, atol=0.01)

    reverse.setParms(dict(from_min=0, from_max=1))
    split.setNamedInput("vector", coords, "generated")
    generated = fog_bands(render_fog(), "combined", 0)
    fog_session.write_exr(dest / "fog-generated.exr")
    measurements = dict(
        name="Generated volume height matches explicit SOP bounds and independent extinction",
        world_y=world_y.tolist(),
        surface_density=surface_density.tolist(),
        expected_transmission=expected_transmission.tolist(),
        explicit=explicit.tolist(),
        generated=generated.tolist(),
    )
    print(json.dumps(measurements))
    np.testing.assert_allclose(generated, explicit, rtol=0, atol=0.01)
    checks.append(measurements)

    # A recooked box must replace the previously uploaded bounding-box transform.
    fog_box.parm("sizey").set(2.2)
    resized_generated = fog_bands(render_fog(), "combined", 0)
    fog_session.write_exr(dest / "fog-generated-resized.exr")
    resized_density = np.interp((4.2 - world_y) / 2.2, (0, 0.5, 1), (0.15, 0.45, 1.3))
    resized_expected = np.exp(-0.8 * resized_density)
    np.testing.assert_allclose(resized_generated, resized_expected, rtol=0, atol=0.01)
    assert np.max(np.abs(resized_generated - generated)) > 0.04
    checks.append(
        dict(
            name="A live SOP resize updates the Generated density profile",
            expected=resized_expected.tolist(),
            generated=resized_generated.tolist(),
        )
    )


report = dict(
    result="passed",
    device=device,
    execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"),
    checks=checks,
    scope="Closed polygon mesh absorption, transparent surface, object scale and packed instances; not a scattering/volume-motion certification.",
)
(dest / "houdini-combined-material-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
