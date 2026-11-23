# SPDX-License-Identifier: Apache-2.0
"""Sky authoring, independent official-Cycles images, updates and safe failures.

Reference XML is independently specified, not an export/replay of our graph.
Logical copy requests are not measurements of physical PCIe traffic.
"""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import native_scene, light_properties, menu_actions, registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/world/sky"
destination.mkdir(parents=True, exist_ok=True)
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
checks = []
report = dict(result="running", device=device, checks=checks)
original_ocio = hou.getenv("OCIO")
original_config = OCIO.GetCurrentConfig()


def pixels(frame):
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
    assert np.isfinite(image).all()
    return image


def record(name, session=None, **details):
    checks.append(
        dict(
            name=name,
            **details,
            **({} if session is None else dict(status=session.status(), diagnostics=session.diagnostics())),
        )
    )


def recover_world(session, update, fragment):
    before = session.status()
    frame = session.wait(session.apply(update))
    after = session.status()
    assert after["state"] == "complete" and not after["error"] and not after.get("update_rejected"), after
    assert any(fragment in issue["message"] for issue in after["source_diagnostics"]), after
    np.testing.assert_allclose(pixels(frame)[0, 0, :3], rop.parmTuple("background").eval(), atol=1e-5, rtol=0)
    if "backend_epoch" in before:
        assert after["backend_alive"]
        assert (after["renderer_pid"], after["backend_epoch"]) == (before["renderer_pid"], before["backend_epoch"])
        assert after["owned_geometry_bytes"] == before["owned_geometry_bytes"]
        assert after["geometry_conversions"] == before["geometry_conversions"]
    return dict(before=before, after=after)


def independent_reference(label, model, direction, properties=None, floor=False, working=None):
    # Default parameters are written explicitly. Native UI degrees and its
    # Y-up rotation are tested against a Z-up direction chosen independently.
    settings = dict(
        sky_type=model,
        sun_disc=model not in ("preetham", "hosek_wilkie"),
        sun_size=math.radians(0.545),
        sun_intensity=1,
        sun_elevation=math.radians(15),
        sun_rotation=0,
        altitude=100,
        air_density=1,
        aerosol_density=1,
        ozone_density=1,
        sun_direction="0 0 1",
        turbidity=2.2,
        ground_albedo=0.3,
    )
    settings.update(properties or {})
    attributes = " ".join(
        f'{name}="{str(value).lower() if isinstance(value, bool) else value}"' for name, value in settings.items()
    )
    vector = " ".join(str(value) for value in direction)
    camera = '<camera width="96" height="64" type="orthographic" />'
    source = (
        '<combine_xyz name="direction" x="'
        + str(direction[0])
        + '" y="'
        + str(direction[1])
        + '" z="'
        + str(direction[2])
        + '" />'
        '<connect from="direction vector" to="sky vector" />'
    )
    strength = 1
    geometry = ""
    if floor:
        # Column-major XML matrix: right +X, screen up -Z, forward -Y.
        camera = '<transform matrix="1 0 0 0 0 0 -1 0 0 -1 0 0 0 5 0 1"><camera width="96" height="64" type="orthographic" /></transform>'
        source = ""
        attributes += ' tex_mapping.rotation="1.5707963267948966 0 0" tex_mapping.type="point"'
        strength = 0.001
        geometry = (
            '<shader name="white"><diffuse_bsdf name="white_bsdf" color="1 1 1" roughness="0" />'
            '<connect from="white_bsdf bsdf" to="output surface" /></shader>'
            '<state shader="white"><mesh P="-10 0 -10 -10 0 10 10 0 10 10 0 -10" nverts="4" verts="0 1 2 3" /></state>'
            '<state shader="sky_world"><light light_type="background" use_mis="true" map_resolution="0" /></state>'
        )
    xml = destination / (label + ".xml")
    image = destination / (label + ".exr")
    xml.write_text(
        "<cycles>" + camera + '<film exposure="1" />'
        '<background name="sky_world"><sky_texture name="sky" '
        + attributes
        + " />"
        + source
        + '<background name="radiance" strength="'
        + str(strength)
        + '" />'
        '<connect from="sky color" to="radiance color" /><connect from="radiance background" to="output surface" />'
        "</background>" + geometry + "</cycles>",
        encoding="utf-8",
    )
    started = time.time_ns()
    run = subprocess.run(
        [
            str(runtime_executable("cycles.exe")),
            "--background",
            "--quiet",
            "--device",
            "CPU",
            "--samples",
            "2048" if floor else "4",
            "--output",
            str(image),
            str(xml),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=dict(os.environ, OCIO=str(working)),
    )
    (destination / (label + ".log")).write_text(run.stdout + run.stderr, encoding="utf-8")
    assert run.returncode == 0 and image.is_file() and image.stat().st_mtime_ns >= started, (
        run.returncode,
        run.stdout,
        run.stderr,
    )
    result = OIIO.ImageBuf(str(image)).get_pixels(OIIO.FLOAT)
    assert result is not None and np.isfinite(result).all(), str(image)
    return result


try:
    config = OCIO.Config.CreateFromStream(original_config.serialize())
    config.setWorkingDir(original_config.getWorkingDir())
    config.setRole(OCIO.ROLE_SCENE_LINEAR, "Linear Rec.709 (sRGB)")
    config.validate()
    working = destination / "sky-rec709.ocio"
    working.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(working))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()

    scene = hou.node("/obj").createNode("subnet", "sky_fixture")
    camera = scene.createNode("cam")
    camera.setParms(dict(tz=5, rx=30, projection=1, orthowidth=4))
    obj = scene.createNode("geo", "retained_receiver", run_init_scripts=False)
    grid = obj.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((2, 2))
    grid.setRenderFlag(True)
    obj.parm("tx").set(20)
    world = menu_actions.create_sky_world()
    sky = world.node("sky_texture")
    assert world.type().name() == "cycles::world_material::1"
    assert world.node("background_shader").input(0) == sky
    assert sky.parm("vector") is None
    assert abs(sky.evalParm("sun_size") - 0.545) < 0.001 and abs(sky.evalParm("sun_elevation") - 15) < 0.0001
    assert sky.parmTuple("sky_rotation").eval() == (90, 0, 0)
    sky.updateParmStates()
    assert sky.parm("sun_disc").isHidden() is False and sky.parmTuple("sun_direction")[0].isHidden()
    assert sky.parm("ground_albedo").isHidden()
    sky.parm("sun_disc").set(0)
    sky.updateParmStates()
    assert sky.parm("sun_size").isHidden() and sky.parm("sun_intensity").isHidden()
    sky.setParms(dict(sky_type="hosek_wilkie"))
    sky.updateParmStates()
    assert sky.parm("sun_elevation").isHidden() and not sky.parm("ground_albedo").isHidden()
    sky.setParms(dict(sky_type="multiple_scattering", sun_disc=1))
    graph = ch.material_graph(world)
    parameters = next(node["parameters"] for node in graph["nodes"] if node["definition"] == "cycles/sky_texture/1")
    assert np.allclose(parameters["tex_mapping.rotation"], (math.pi / 2, 0, 0))
    assert parameters["tex_mapping.type"] == "point"
    assert abs(parameters["sun_elevation"] - math.pi / 12) < 1e-7
    reflected = next(item for item in ch.shader_catalogue()["definitions"] if item["engine_type"] == "sky_texture")
    assert reflected["world_graph_adapter"] and reflected["surface_graph_adapter"] and reflected["volume_graph_adapter"]
    record("Native controls use degrees, Y up, friendly model labels and model-dependent visibility")

    # Current optional environment properties preserve authored channels.
    env = scene.createNode("envlight")
    light_properties.install(env)
    env.parm("cycles_environment_map_resolution").set(512)
    env.parm("cycles_environment_map_resolution").setExpression("128 * 4", hou.exprLanguage.Hscript)
    assert not light_properties.install(env) and env.evalParm("cycles_environment_map_resolution") == 512
    assert env.parm("cycles_environment_map_resolution").expression() == "128 * 4"
    assert not light_properties.install(env)
    control = env.parm("cycles_environment_map_resolution").parmTemplate()
    assert control.menuUseToken() and control.menuItems()[0] == "0" and control.menuLabels()[0] == "Auto"
    assert control.tags()["sidefx::slider"] == "none"
    assert env.parm("cycles_environment_map_resolution").containingFolders()[-1] == "Sampling"
    env.destroy()
    record("Native environment sampling uses valid presets and retains existing resolution channels")

    rop = hou.node("/out").createNode("cycles_ipr")
    rop.setParms(
        dict(
            source_mode="objects",
            objectroot=scene.path(),
            vobject="*",
            lights="",
            camera=camera.path(),
            resx=96,
            resy=64,
            samples=64,
            world_source="material",
            world_material=world.path(),
            world_sampling="none",
            use_material_assignments=0,
            material_kind="diffuse",
            clamp_indirect=0,
            ipr_samples=64,
            use_denoise=0,
            ipr_use_denoise=0,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
        )
    )
    rop.parmTuple("basecolor").set((1, 1, 1))
    with ch.Session(device=device, width=96, height=64, samples=64, collect_diagnostics=True) as session:

        def render():
            return session.wait(session.apply(native_scene.capture(rop), hou.time()))

        def retained():
            assert session.status()["geometry_conversions"] == 1
            copies = session.diagnostics()["buffer_copy_requests"]
            assert not any(name in copies for name in ("tri_vindex", "tri_verts", "tri_shader")), copies

        # An orthographic camera rotated 30 degrees looks (0, .5, -sqrt(3)/2).
        # The native sky's +90 X rotation makes the sky direction (0,sqrt(3)/2,.5).
        direction = (0, math.sqrt(3) / 2, 0.5)
        for model in ("multiple_scattering", "single_scattering", "preetham", "hosek_wilkie"):
            sky.parm("sky_type").set(model)
            frame = render()
            measured = pixels(frame)[32, 48, :3]
            reference = independent_reference(model, model, direction, working=working)[32, 48, :3]
            np.testing.assert_allclose(measured, reference, rtol=0.002, atol=0.002)
            assert session.status()["geometry_conversions"] == 1
            record(
                "Official standalone reference: " + model,
                session,
                measured=measured.tolist(),
                expected=reference.tolist(),
            )

        sky.setParms(dict(sky_type="multiple_scattering", sun_elevation=30, sun_size=8, sun_intensity=2))
        bright = pixels(render())[32, 48, :3]
        reference = independent_reference(
            "solar-disc",
            "multiple_scattering",
            direction,
            dict(sun_elevation=math.radians(30), sun_size=math.radians(8), sun_intensity=2),
            working=working,
        )[32, 48, :3]
        np.testing.assert_allclose(bright, reference, rtol=0.002, atol=0.01)
        retained()
        sky.parm("sun_disc").set(0)
        without_sun = pixels(render())[32, 48, :3]
        retained()
        assert bright.max() > without_sun.max() * 5, (bright, without_sun)
        record("Solar-disc size, position, intensity and disabling match independently chosen directions", session)

        sky.setParms(
            dict(
                sun_disc=1,
                sun_size=0.545,
                sun_elevation=15,
                sun_intensity=1,
                altitude=1200,
                air_density=0.7,
                aerosol_density=2,
                ozone_density=1.5,
            )
        )
        measured = pixels(render())[32, 48, :3]
        reference = independent_reference(
            "atmosphere",
            "multiple_scattering",
            direction,
            dict(altitude=1200, air_density=0.7, aerosol_density=2, ozone_density=1.5),
            working=working,
        )[32, 48, :3]
        np.testing.assert_allclose(measured, reference, rtol=0.002, atol=0.002)
        retained()
        record("Atmospheric parameters and metre altitude match official Cycles", session)

        sky.parmTuple("sky_rotation").set((0, 0, 0))
        measured = pixels(render())[32, 48, :3]
        reference = independent_reference(
            "z-up",
            "multiple_scattering",
            (0, 0.5, -math.sqrt(3) / 2),
            dict(altitude=1200, air_density=0.7, aerosol_density=2, ozone_density=1.5),
            working=working,
        )[32, 48, :3]
        np.testing.assert_allclose(measured, reference, rtol=0.002, atol=0.002)
        retained()
        sky.parmTuple("sky_rotation").set((90, 0, 0))
        sky.setParms(dict(altitude=100, air_density=1, aerosol_density=1, ozone_density=1))
        rop.parm("world_sampling").set("auto")
        render()
        retained()
        copies = session.diagnostics()["buffer_copy_requests"]
        # Timed/displacement shader evaluation uses the current 32-byte
        # KernelShaderEvalInput, also checked by world_sampling_smoke.
        report["sampling_layout"] = dict(input_stride_bytes=32, modern_resolution=(512, 256))
        assert copies["ShaderEval input"]["requested_bytes"] == 512 * 256 * 32, copies
        assert copies["light_background_conditional_cdf"]["requested_bytes"] == 513 * 256 * 8
        builds = session.diagnostics()["background_builds"]
        camera.parm("tx").set(0.1)
        render()
        retained()
        assert session.diagnostics()["background_builds"] == builds
        assert "light_background_conditional_cdf" not in session.diagnostics()["buffer_copy_requests"]
        sky.parm("sky_type").set("preetham")
        render()
        retained()
        assert session.diagnostics()["buffer_copy_requests"]["ShaderEval input"]["requested_bytes"] == 1024 * 512 * 32
        record("Native sky retains direct sun sampling; legacy skies avoid the modern solar-disc sampler", session)

        # Invalid source shading accepts a configured-background fallback and
        # diagnostics without discarding usable geometry or its renderer.
        sky.parm("sky_type").set("multiple_scattering")
        render()
        failures = []
        for key, value in (("air_density", -1), ("sun_size", 4), ("altitude", -1)):
            bad = ch.material_graph(world)
            next(node["parameters"] for node in bad["nodes"] if node["definition"] == "cycles/sky_texture/1")[key] = (
                value
            )
            failures.append(dict(parameter=key, **recover_world(session, dict(world=dict(graph=bad)), "Sky Texture")))
            render()
        bad = ch.material_graph(world)
        p = next(node["parameters"] for node in bad["nodes"] if node["definition"] == "cycles/sky_texture/1")
        p.update(sky_type="hosek_wilkie", sun_direction=(0, 0, 0))
        failures.append(
            dict(parameter="sun_direction", **recover_world(session, dict(world=dict(graph=bad)), "Sun Direction"))
        )
        render()
        render()
        retained()
        record(
            "Invalid sky values use configured background; repair clears warnings and retains geometry",
            session,
            failures=failures,
        )

        # A real SOP plane, independently specified Cycles reference geometry,
        # and sampled direct/indirect light verify more than visible background.
        transform = obj.createNode("xform")
        transform.setInput(0, grid)
        transform.parm("rx").set(-90)
        transform.parmTuple("s").set((10, 10, 10))
        transform.setRenderFlag(True)
        obj.parm("tx").set(0)
        camera.setParms(dict(tx=0, ty=5, tz=0, rx=-90))
        sky.setParms(dict(sky_type="multiple_scattering", sun_elevation=45, sun_size=2))
        world.node("background_shader").parm("strength").set(0.001)
        rop.setParms(dict(samples=2048, world_sampling="auto"))
        measured = pixels(render())[20:44, 30:66, :3].mean((0, 1))
        reference = independent_reference(
            "diffuse-floor",
            "multiple_scattering",
            (0, 0, 1),
            dict(sun_elevation=math.radians(45), sun_size=math.radians(2)),
            floor=True,
            working=working,
        )[20:44, 30:66, :3].mean((0, 1))
        record(
            "A native SOP diffuse plane receives sky and sun illumination matching independent Cycles geometry",
            session,
            measured=measured.tolist(),
            expected=reference.tolist(),
            comparison="2048-sample ROI mean, statistical tolerance",
        )
        assert measured.min() > 0
        np.testing.assert_allclose(measured, reference, rtol=0.06, atol=0.015)
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0

    # Sky is also a texture input to ordinary surface and volume materials.
    # Test those advertised domains, including the surface Generated default.
    surface = ch.create_material(name="sky_surface", shader="emission")
    texture = surface.createNode("cycles::sky_texture::1")
    texture.parm("sky_type").set("preetham")
    texture.parmTuple("sky_rotation").set((0, 0, 0))
    coordinates = surface.createNode("cycles::combine_xyz::1")
    coordinates.setParms(dict(x=0.6, y=0, z=0.8))
    texture.setNamedInput("vector", coordinates, "vector")
    surface.node("surface_shader").setNamedInput("color", texture, "color")
    surface.node("surface_shader").parm("strength").set(1)
    obj.parm("shop_materialpath").set(surface.path())
    rop.setParms(dict(world_source="color", vobject=obj.name(), use_material_assignments=1, samples=64))
    rop.parmTuple("background").set((0, 0, 0))
    expected = independent_reference("surface-input", "preetham", (0.6, 0, 0.8), working=working)[32, 48, :3]
    with ch.Session(device=device, width=96, height=64, samples=64, collect_diagnostics=True) as session:
        measured = pixels(session.wait(session.apply(native_scene.capture(rop))))[32, 48, :3]
        np.testing.assert_allclose(measured, expected, rtol=0.002, atol=0.002)
        texture.setInput(texture.inputNames().index("vector"), None)
        assert not texture.inputConnections()
        measured = pixels(session.wait(session.apply(native_scene.capture(rop))))[31:33, 47:49, :3].mean((0, 1))
        # Bounds-normalized coordinates are not unit directions. Upstream's
        # legacy sky evaluates the supplied vector without normalizing it.
        generated = independent_reference("surface-generated", "preetham", (0.5, 0, 0.5), working=working)[32, 48, :3]
        assert np.max(np.abs(measured - expected)) > 0.01, (
            "Disconnecting Vector retained its previous directional result"
        )
        np.testing.assert_allclose(measured, generated, rtol=0.008, atol=0.005)
        record("Surface Sky Texture supports explicit directions and normalized nominal SOP bounds", session)

    fixture = destination / "sky-density.vdb"
    subprocess.run(
        [str(runtime_executable("ch_harness.exe")), "--write-volume-fixture", str(fixture)], check=True, timeout=30
    )
    volume_object = scene.createNode("geo", "sky_volume", run_init_scripts=False)
    source = volume_object.createNode("file")
    source.parm("file").set(str(fixture))
    source.setRenderFlag(True)
    volume = hou.node("/mat").createNode("cycles::volume_material::1", "sky_volume_material")
    emit = volume.createNode("cycles::emission::1")
    emit.parm("strength").set(1)
    field = volume.createNode("cycles::attribute::1")
    field.parm("attribute").set("density")
    emit.setNamedInput("strength", field, "fac")
    volume.node("volume_output").setNamedInput("volume", emit, "emission")
    texture = volume.createNode("cycles::sky_texture::1")
    texture.parm("sky_type").set("preetham")
    texture.parmTuple("sky_rotation").set((0, 0, 0))
    coordinates = volume.createNode("cycles::combine_xyz::1")
    coordinates.setParms(dict(x=0, y=math.sqrt(3) / 2, z=0.5))
    texture.setNamedInput("vector", coordinates, "vector")
    emit.setNamedInput("color", texture, "color")
    volume_object.parm("shop_materialpath").set(volume.path())
    camera.setParms(dict(ty=0, tz=5, rx=0))
    rop.setParms(dict(vobject=volume_object.name(), samples=512))
    expected = independent_reference("volume-input", "preetham", (0, math.sqrt(3) / 2, 0.5), working=working)[
        32, 48, :3
    ]
    with ch.Session(device=device, width=96, height=64, samples=512, collect_diagnostics=True) as session:
        measured = pixels(session.wait(session.apply(native_scene.capture(rop))))[28:36, 44:52, :3].mean((0, 1))
        record(
            "Volume sky emission integral comparison",
            session,
            measured=measured.tolist(),
            expected=(expected * 9 / 8).tolist(),
            comparison="512 samples, 8-by-8 interior ROI",
        )
        np.testing.assert_allclose(measured, expected * 9 / 8, rtol=0.01, atol=0.005)
        texture.parm("sky_type").set("hosek_wilkie")
        texture.parmTuple("sun_direction").set((0, 0, 0))
        resources = session.status()["geometry_resources"]
        dark = pixels(session.wait(session.apply(native_scene.capture(rop))))
        status = session.status()
        assert status["state"] == "complete" and not status["error"] and status["geometry_resources"] == resources, (
            status
        )
        assert any(
            issue["code"] == "material.volume_source_unavailable" and "Sky Texture" in issue["message"]
            for issue in status["source_diagnostics"]
        ), status
        np.testing.assert_allclose(dark[28:36, 44:52, :3], 0, atol=1e-6, rtol=0)
        rop.parmTuple("background").set((1, 1, 1))
        lit = pixels(session.wait(session.apply(native_scene.capture(rop))))[28:36, 44:52, :3].mean((0, 1))
        assert lit[0] > lit[1] + 0.02 and lit[2] > lit[1] + 0.02, lit
        rop.parmTuple("background").set((0, 0, 0))
        texture.parmTuple("sun_direction").set((0, 0, 1))
        texture.parm("sky_type").set("preetham")
        repaired = pixels(session.wait(session.apply(native_scene.capture(rop))))[28:36, 44:52, :3].mean((0, 1))
        np.testing.assert_allclose(repaired, expected * 9 / 8, rtol=0.01, atol=0.005)
        assert not session.status()["source_diagnostics"], session.status()
        record(
            "Invalid volume sky values retain density, scatter without emission, and repair",
            session,
            diagnostic_rgb=lit.tolist(),
        )
        texture.setInput(texture.inputNames().index("vector"), None)
        resources = session.status()["geometry_resources"]
        pixels(session.wait(session.apply(native_scene.capture(rop))))
        status = session.status()
        assert status["state"] == "complete" and not status["error"] and status["source_diagnostics"], status
        assert status["geometry_resources"] == resources and status["owned_geometry_bytes"] > 0, status
        record("Missing volume sky coordinates retain usable density and complete with a local diagnostic", session)
        texture.setNamedInput("vector", coordinates, "vector")
        repaired = pixels(session.wait(session.apply(native_scene.capture(rop))))[28:36, 44:52, :3].mean((0, 1))
        np.testing.assert_allclose(repaired, expected * 9 / 8, rtol=0.01, atol=0.005)
        assert not session.status()["source_diagnostics"], session.status()
        record("Repairing the volume direction restores independent sky radiance and clears the diagnostic", session)
        emit.setInput(emit.inputNames().index("color"), None)
        emit.parmTuple("color").set((1, 1, 1))
        measured = pixels(session.wait(session.apply(native_scene.capture(rop))))[28:36, 44:52, :3].mean((0, 1))
        np.testing.assert_allclose(measured, (9 / 8,) * 3, rtol=0.01, atol=0.005)
        record("An unused Sky Texture does not add volume coordinate requirements", session)

    # Promoted sun controls retain degree semantics through animation/HIP reload.
    control = ch.promote_parameter(world, sky, "sun_elevation", name="solar_elevation")
    orientation = ch.promote_parameter(world, sky, "sky_rotation", name="world_orientation")
    orientation[1].setExpression("10 * ($F - 1)", hou.exprLanguage.Hscript)
    for frame, value in ((1, 15), (5, 45)):
        key = hou.Keyframe(value)
        key.setFrame(frame)
        control[0].setKeyframe(key)
        rotation = hou.Keyframe(0 if frame == 1 else 30)
        rotation.setFrame(frame)
        orientation[2].setKeyframe(rotation)
    saved = destination / "sky-material-native.hiplc"
    world_path = world.path()
    hou.hipFile.save(str(saved))
    registry.close_all()
    hou.hipFile.load(str(saved), suppress_save_prompt=True)
    world = hou.node(world_path)
    first = ch.material_graph(world, hou.frameToTime(1))
    last = ch.material_graph(world, hou.frameToTime(5))
    value = lambda graph: next(
        node["parameters"]["sun_elevation"] for node in graph["nodes"] if node["definition"] == "cycles/sky_texture/1"
    )
    assert abs(value(first) - math.pi / 12) < 1e-6 and abs(value(last) - math.pi / 4) < 1e-6
    rotation = lambda graph: next(
        node["parameters"]["tex_mapping.rotation"]
        for node in graph["nodes"]
        if node["definition"] == "cycles/sky_texture/1"
    )
    assert abs(rotation(first)[2]) < 1e-6 and abs(rotation(last)[2] - math.pi / 6) < 1e-6
    assert abs(rotation(first)[0] - math.pi / 2) < 1e-6
    assert abs(rotation(first)[1]) < 1e-6 and abs(rotation(last)[1] - math.radians(40)) < 1e-6
    record("Sun and Sky Rotation promotion, frame animation and HIP reload preserve native degree semantics")
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    if original_ocio is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_ocio)
    hou.Color.reloadOCIO()
    (destination / "houdini-sky-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
