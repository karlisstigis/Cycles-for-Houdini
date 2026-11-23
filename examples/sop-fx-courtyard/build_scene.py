# SPDX-License-Identifier: Apache-2.0
"""Build and optionally render an editable native SOP Cycles demonstration."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

import hou
import numpy as np
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import native_scene, light_properties

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("CYCLES_HOUDINI_SOURCE_ROOT", ROOT))
DESTINATION = (
    Path(os.environ.get("CYCLES_HOUDINI_TEST_ARTIFACTS", DATA_ROOT / "artifacts")) / "examples/sop-fx-courtyard"
)


def material(name, color, metallic=0.0, roughness=0.5, emission=None):
    builder = ch.create_material(name=name, shader="emission" if emission else "principled_bsdf")
    shader = builder.node("surface_shader")
    if emission:
        shader.parmTuple("color").set(color)
        shader.parm("strength").set(emission)
    else:
        shader.parmTuple("base_color").set(color)
        shader.parm("metallic").set(metallic)
        shader.parm("roughness").set(roughness)
    return builder


def build():
    build_started = time.perf_counter()
    DESTINATION.mkdir(parents=True, exist_ok=True)
    if hou.node("/obj/fx_courtyard") or hou.node("/out/cycles_sop_courtyard"):
        raise RuntimeError("Generate the example in a fresh Houdini scene; existing artist nodes are preserved.")
    hou.setFrame(1)
    hou.setFps(24)
    configuration = OCIO.Config.CreateFromStream(OCIO.GetCurrentConfig().serialize())
    configuration.setWorkingDir(OCIO.GetCurrentConfig().getWorkingDir())
    configuration.setRole(OCIO.ROLE_SCENE_LINEAR, "Linear Rec.709 (sRGB)")
    configuration.validate()
    profile = DESTINATION / "showcase.ocio"
    profile.write_text(configuration.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(profile))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()

    brass = material("courtyard_burnished_brass", (0.43, 0.22, 0.055), 0.85, 0.24)
    steel = material("courtyard_blue_steel", (0.035, 0.09, 0.14), 0.78, 0.32)
    ceramic = material("courtyard_ivory_ceramic", (0.56, 0.52, 0.43), 0.05, 0.36)
    glow = material("courtyard_cyan_emission", (0.03, 0.65, 1.0), emission=6.0)
    hot = material("courtyard_amber_emission", (1.0, 0.19, 0.015), emission=5.0)
    grass = material("courtyard_copper_strands", (0.075, 0.19, 0.13), 0.22, 0.4)
    stone = material("courtyard_noise_displaced_stone", (0.2, 0.2, 0.2), 0.03, 0.72)
    noise = stone.createNode("cycles::noise_texture::1", "stone_noise")
    coordinates = stone.createNode("cycles::uvmap::1", "stone_coordinates")
    noise.setNamedInput("vector", coordinates, "UV")
    for name, value in dict(
        dimensions="3D", type="fBM", scale=0.72, detail=4.0, roughness=0.68, distortion=0.24
    ).items():
        noise.parm(name).set(value)
    ramp = stone.createNode("cycles::color_ramp::1", "stone_palette")
    ramp.parm("ramp").set(
        hou.Ramp(
            (hou.rampBasis.Linear,) * 4,
            (0.15, 0.4, 0.65, 0.9),
            ((0.018, 0.026, 0.031), (0.10, 0.13, 0.13), (0.26, 0.22, 0.15), (0.44, 0.39, 0.27)),
        )
    )
    ramp.setNamedInput("factor", noise, "fac")
    stone.node("surface_shader").setNamedInput("base_color", ramp, "color")
    offset = stone.createNode("cycles::displacement::1", "true_stone_displacement")
    offset.parm("space").set("object")
    offset.parm("scale").set(0.68)
    offset.parm("midlevel").set(0.5)
    offset.setNamedInput("height", noise, "fac")
    stone.node("surface_output").setNamedInput("displacement", offset, "displacement")
    stone.parm("cycles_displacement_method").set("both")
    # Fine noise supplies bump detail separately from the larger geometric relief.
    fine = stone.createNode("cycles::noise_texture::1", "fine_stone_grain")
    fine.setNamedInput("vector", coordinates, "UV")
    fine.parm("scale").set(18.0)
    fine.parm("detail").set(2.5)
    bump = stone.createNode("cycles::bump::1", "fine_grain_normal")
    bump.parm("distance").set(0.035)
    bump.parm("strength").set(0.5)
    bump.setNamedInput("height", fine, "fac")
    stone.node("surface_shader").setNamedInput("normal", bump, "normal")
    ch.promote_parameter(stone, noise, "scale", name="rock_noise_scale", label="Large noise scale")
    ch.promote_parameter(stone, offset, "scale", name="rock_relief", label="True displacement scale")

    glass = ch.create_material(name="courtyard_optical_glass", shader="glass_bsdf")
    glass.node("surface_shader").parmTuple("color").set((0.91, 0.98, 1.0))
    glass.node("surface_shader").parm("IOR").set(1.46)
    glass.node("surface_shader").parm("roughness").set(0.025)
    sparks = material("courtyard_sparks", (1, 0.4, 0.03), emission=9.0)
    attribute = sparks.createNode("cycles::attribute::1", "particle_color")
    attribute.parm("attribute").set("Cd")
    sparks.node("surface_shader").setNamedInput("color", attribute, "color")
    medium = ch.create_material(name="courtyard_procedural_plume", shader="principled_volume")
    v = medium.node("volume_shader")
    v.parm("density").set(0.75)
    v.parmTuple("color").set((0.30, 0.38, 0.46))
    v.parm("anisotropy").set(0.15)
    v.parm("blackbody_intensity").set(0.035)
    v.parm("temperature").set(1450.0)
    v.parmTuple("emission_color").set((1.0, 0.15, 0.008))
    field = medium.createNode("cycles::attribute::1", "emission_grid")
    field.parm("attribute").set("emission")
    gain = medium.createNode("cycles::math::1", "plume_glow")
    gain.parm("math_type").set("multiply")
    gain.parm("value2").set(2.5)
    gain.setNamedInput("value1", field, "fac")
    v.setNamedInput("emission_strength", gain, "value")

    obj = hou.node("/obj").createNode("geo", "fx_courtyard")
    for child in obj.children():
        child.destroy()
    # Native nodes do the cooking; Python only constructs the editable network.
    import runpy

    native = runpy.run_path(str(ROOT / "examples/sop-fx-courtyard/native_geometry.py"))["geometry"](
        obj,
        dict(
            brass=brass,
            steel=steel,
            ceramic=ceramic,
            glow=glow,
            hot=hot,
            grass=grass,
            stone=stone,
            glass=glass,
            sparks=sparks,
        ),
    )
    render_branches = native["branches"]
    ground, hero, rocks, strands, particles = [
        native[name] for name in ("ground", "hero", "rocks", "strands", "particles")
    ]
    plume_path = DESTINATION / "procedural-plume.vdb"
    if os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY"):
        harness = runpy.run_path(str(ROOT / "tests/native_test_runtime.py"))["runtime_executable"]()
    else:
        harness = ROOT / "build" / f"windows-h{hou.applicationVersionString()}-release" / "bin/ch_harness.exe"
    subprocess.run([str(harness), "--write-showcase-volume", str(plume_path)], check=True)
    file = obj.createNode("file", "LOAD_PROCEDURAL_PLUME")
    file.parm("file").set(str(plume_path))
    embedded = obj.createNode("stash", "EMBEDDED_VDB_PLUME")
    embedded.parm("stash").set(file.geometry())
    file.destroy()
    move = obj.createNode("xform", "PLUME_POSITION")
    move.setInput(0, embedded)
    move.parmTuple("t").set((-1.7, 0.4, -0.35))
    binding = obj.createNode("material", "PLUME_MATERIAL")
    binding.setInput(0, move)
    binding.parm("shop_materialpath1").set(medium.path())
    render_branches.append(binding)

    merge = obj.createNode("merge", "MIXED_SOP_CONTENT")
    for i, sop in enumerate(render_branches):
        merge.setInput(i, sop)
    out = obj.createNode("null", "RENDER_COURTYARD")
    out.setInput(0, merge)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    geometry = out.geometry()
    errors = {node.path(): node.errors() for node in obj.children() if node.errors()}
    if geometry is None or errors:
        raise RuntimeError("Example SOP cooking failed: " + json.dumps(errors))
    assert len(geometry.prims()) > 50, "Expected the full packed instance population"
    obj.layoutChildren()
    for builder in (brass, steel, ceramic, glow, hot, grass, stone, glass, sparks, medium):
        builder.layoutChildren()
    hou.node("/mat").layoutChildren()

    camera = hou.node("/obj").createNode("cam", "courtyard_camera")
    position = hou.Vector3(10, 7.5, 14)
    target = hou.Vector3(0, 2.0, -0.25)
    back = (position - target).normalized()
    right = hou.Vector3(0, 1, 0).cross(back).normalized()
    up = back.cross(right)
    camera.setWorldTransform(hou.Matrix4((*right, 0, *up, 0, *back, 0, *position, 1)))
    camera.parm("focal").set(38)
    camera.parm("aperture").set(36)
    camera.parmTuple("res").set((960, 540))
    light = hou.node("/obj").createNode(light_properties.LIGHT_TYPE, "courtyard_key_light")
    light.setParms(dict(cycles_light_type="point", cycles_light_radius=0, light_exposure=0, normalizearea=1))
    light.parmTuple("t").set((-3.5, 7.0, 4.5))
    # Cycles' zero-radius point area is 4; normalized power therefore needs
    # four times the legacy example's unnormalized strength for the same image.
    light.parmTuple("cycles_light_color").set((1.0, 6500 / 8500, 4500 / 8500))
    light.parm("light_intensity").set(34000)
    rop = hou.node("/out").createNode("cycles_ipr", "cycles_sop_courtyard")
    set_rop_devices(rop, ch.default_device())
    for name, value in dict(
        source_mode="objects",
        vobject="*",
        lights="*",
        soppath=out.path(),
        camera=camera.path(),
        lightpath=light.path(),
        resx=960,
        resy=540,
        samples=128,
        use_material_assignments=1,
    ).items():
        rop.parm(name).set(value)
    rop.parmTuple("lightstrength").set((8500.0, 6500.0, 4500.0))
    rop.parmTuple("background").set((0.025, 0.04, 0.065))
    rop.parm("picture").set("$HIP/courtyard.$F4.exr")
    out.setComment(
        "Render this SOP with /out/cycles_sop_courtyard. Unpacked meshes, curves, particles and an embedded procedural VDB share this SOP with 54 genuinely instanced packed panels."
    )
    stone.setComment(
        "Edit Large noise scale and True displacement scale here, or dive inside to edit Noise, ramps and bump. True displacement changes renderer vertices; the coarse SOP is preserved."
    )
    obj.setComment(
        "Native OBJ/SOP Cycles example. Dive inside for geometry controls; /mat contains editable Cycles materials. The VDB plume is procedural, not a simulation benchmark."
    )
    # Preserve the original artist test-drive file, including saved artist edits.
    example = DESTINATION / "sop-fx-courtyard-current.hiplc"
    hou.hipFile.save(str(example))
    report = dict(
        example=str(example),
        rop=rop.path(),
        render_sop=out.path(),
        packed_primitives=sum(p.type() == hou.primType.PackedGeometry for p in geometry.prims()),
        primitive_types=sorted({str(p.type()) for p in geometry.prims()}),
        source_points=len(geometry.points()),
        source_primitives=len(geometry.prims()),
        materials=10,
        python_sops=[node.path() for node in obj.children() if node.type().name() == "python"],
        profile=str(profile),
        build_and_cook_seconds=time.perf_counter() - build_started,
        controls=[ground.path(), hero.path(), rocks.path(), strands.path(), particles.path(), stone.path()],
        scope="Native SOP demonstration; procedural plume, not a simulated production fixture",
    )
    (DESTINATION / "scene.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return rop, report


def render(rop, width, height, samples, device):
    rop.parm("resx").set(width)
    rop.parm("resy").set(height)
    rop.parm("samples").set(samples)
    set_rop_devices(rop, device)
    before = time.perf_counter()
    configuration = dict(
        zip(
            ("device", "width", "height", "samples", "use_texture_cache", "auto_texture_cache"),
            native_scene.configuration(rop),
        )
    )
    with ch.Session(**configuration, interactive=False, collect_diagnostics=True) as session:
        frame = session.wait(session.apply(native_scene.capture(rop)), timeout=240)
        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(height, width, 4)
        assert np.isfinite(pixels).all() and np.std(pixels[:, :, :3]) > 0.05
        status = session.status()
        assert status["packed_instances"] > 50 and status["material_resources"] >= 10, status
        output = DESTINATION / ("courtyard-cpu.exr" if device == "CPU" else "courtyard-optix.exr")
        session.write_exr(output)
        image = session.display_image()
        # The renderer's existing OCIO display route creates this presentation
        # preview. The EXR remains associated, scene-linear float32.
        from PySide6 import QtGui

        qimage = QtGui.QImage(
            image["rgba"], image["width"], image["height"], image["width"] * 4, QtGui.QImage.Format_RGBA8888
        )
        preview = DESTINATION / ("courtyard-cpu.png" if device == "CPU" else "courtyard-optix.png")
        assert qimage.save(str(preview))
        report = dict(
            result="passed",
            device=device,
            width=width,
            height=height,
            samples=samples,
            wall_seconds=time.perf_counter() - before,
            status=status,
            diagnostics=session.diagnostics(),
            output=str(output),
            preview=str(preview),
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    (DESTINATION / ("render-cpu.json" if device == "CPU" else "render-optix.json")).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--device", default="")
    arguments = parser.parse_args()
    rop, report = build()
    if arguments.render:
        report["render"] = render(
            rop, arguments.width, arguments.height, arguments.samples, arguments.device or ch.default_device()
        )
    print(json.dumps(report, indent=2))
