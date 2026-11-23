# SPDX-License-Identifier: Apache-2.0
"""Independent native compositing expectations, including zero-alpha emission."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/transparency"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_env = hou.getenv("OCIO")
emitted = np.asarray((0.4, 0.2, 0.1), dtype=np.float32)
background = (0.05, 0.1, 0.15)


def profile(working, name):
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
    display = OCIO.ColorSpace(name="Native Alpha Display")
    display.setTransform(
        OCIO.MatrixTransform(matrix=[0.5, 0, 0, 0, 0, 0.75, 0, 0, 0, 0, 0.25, 0, 0, 0, 0, 0.125]),
        OCIO.COLORSPACE_DIR_FROM_REFERENCE,
    )
    config.addColorSpace(display)
    config.addDisplayView("Native Alpha", "Coverage", display.getName(), "")
    config.setActiveDisplays("Native Alpha")
    config.setActiveViews("Coverage")
    config.validate()
    path = destination / (name + ".ocio")
    path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(path))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
    return OCIO.GetCurrentConfig()


def rgba(frame):
    result = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(
        frame["height"], frame["width"], 4
    )
    assert np.isfinite(result).all()
    assert result[..., 3].min() >= -1e-6 and result[..., 3].max() <= 1 + 1e-6
    return result


def check_output(session, frame, name):
    path = destination / (name + ".exr")
    session.write_exr(path)
    reader = OIIO.ImageInput.open(str(path))
    assert reader, path
    spec = reader.spec()
    image = reader.read_image(format=OIIO.FLOAT)
    channels = [spec.channelnames.index(c) for c in ("R", "G", "B", "A")]
    np.testing.assert_array_equal(image[..., channels], rgba(frame)[::-1])
    reader.close()
    return str(path)


def check_display(session, frame, processor, label):
    before = session.status()
    source = rgba(frame)
    straight = source[..., :3].copy()
    alpha = source[..., 3]
    nonzero = alpha != 0
    straight[nonzero] /= alpha[nonzero, None]
    expected = np.asarray([processor.applyRGB(pixel.tolist()) for pixel in straight.reshape(-1, 3)])
    expected = expected.reshape(*alpha.shape, 3)
    expected_bytes = np.rint(np.clip(expected, 0, 1) * 255).astype(np.int16)
    over_black = np.asarray([processor.applyRGB(pixel.tolist()) for pixel in source[..., :3].reshape(-1, 3)])
    over_black_bytes = np.rint(np.clip(over_black.reshape(*alpha.shape, 3), 0, 1) * 255).astype(np.int16)
    for channels in ("rgba", "rgb", "alpha"):
        image = session.display_image(display="Native Alpha", view="Coverage", channels=channels)
        actual = np.frombuffer(image["rgba"], np.uint8).reshape(*alpha.shape, 4)[::-1]
        if channels == "alpha":
            matte = np.rint(np.clip(alpha, 0, 1) * 255).astype(np.int16)
            np.testing.assert_array_equal(actual[..., :3], np.repeat(matte[..., None], 3, axis=-1))
            assert np.all(actual[..., 3] == 255)
        elif channels == "rgb":
            # RGB without alpha is the image over black: associated RGB through the view.
            assert np.max(np.abs(actual[..., :3].astype(np.int16) - over_black_bytes)) <= 1, label
            assert np.all(actual[..., 3] == 255), label
        else:
            assert np.max(np.abs(actual[..., :3].astype(np.int16) - expected_bytes)) <= 1, label
            output_alpha = np.rint(np.clip(alpha, 0, 1) * 255).astype(np.uint8) if channels == "rgba" else 255
            assert np.all(actual[..., 3] == output_alpha), label
    assert session.status()["generation"] == before["generation"]
    assert session.status()["geometry_conversions"] == before["geometry_conversions"]
    return dict(name=label, pixels=alpha.size, fractional=int(np.count_nonzero((alpha > 0) & (alpha < 1))))


obj = hou.node("/obj").createNode("geo", "native_alpha_geometry", run_init_scripts=False)
source = obj.createNode("python", "half_plane")
source.parm("python").set("""import hou
g = hou.pwd().geometry(); face = g.createPolygon()
# Houdini polygons are clockwise when viewed from their outward normal.
# This receiver faces +Z, matching the independently constructed Cycles plane.
for position in reversed(((-3.,-3.,0.),(.173,-3.,0.),(.173,3.,0.),(-3.,3.,0.))):
    p = g.createPoint(); p.setPosition(position); face.addVertex(p)
""")
source.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "native_alpha_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
builder = ch.create_material(name="native_alpha_surface", shader="emission")
emit = builder.node("surface_shader")
emit.parmTuple("color").set(emitted.tolist())
emit.parm("strength").set(1)
transparent = builder.createNode("cycles::transparent_bsdf::1", "transparent")
mixed = builder.createNode("cycles::mix_closure::1", "coverage_mix")
mixed.setNamedInput("closure1", emit, "emission")
mixed.setNamedInput("closure2", transparent, "BSDF")
mixed.parm("fac").set(0.375)
glass = ch.create_material(name="native_alpha_glass", shader="glass_bsdf")
# The default this test was written for (new nodes now take Blender's).
glass.node("surface_shader").setParms(dict(distribution="ggx"))
glass.node("surface_shader").parm("IOR").set(1.5)
glass.node("surface_shader").parmTuple("color").set((1, 1, 1))
holdout = ch.create_material(name="native_alpha_holdout", shader="holdout")
diffuse = ch.create_material(name="native_alpha_diffuse", shader="diffuse_bsdf")
diffuse.node("surface_shader").parmTuple("color").set((0.5, 0.5, 0.5))
obj.parm("shop_materialpath").set(builder.path())

fixture = destination / "emission-grid.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-volume-fixture", str(fixture)], check=True)
volume_obj = hou.node("/obj").createNode("geo", "native_alpha_volume", run_init_scripts=False)
file = volume_obj.createNode("file")
file.parm("file").set(str(fixture))
volume = volume_obj.createNode("stash")
volume.parm("stash").set(file.geometry())
file.destroy()
fixture.unlink()
volume.setRenderFlag(True)
medium = ch.create_material(name="native_alpha_emission", shader="principled_volume")
medium.node("volume_shader").destroy()
volume_emit = medium.createNode("cycles::emission::1", "volume_shader")
volume_emit.parmTuple("color").set((0.5, 0.25, 0.125))
volume_emit.parm("strength").set(1)
volume_density = medium.createNode("cycles::volume_info::1", "density_field")
volume_emit.setNamedInput("strength", volume_density, "density")
medium.node("volume_output").setNamedInput("volume", volume_emit, "emission")
volume_obj.parm("shop_materialpath").set(medium.path())


def scene(sop=source, geometry_obj=obj):
    return dict(
        geometry=[ch.geometry_request(geometry_obj, sop, material_mode="native", material="fallback")],
        material_resolver=native_scene.resolve_materials,
        camera_path=camera.path(),
        background=background,
    )


reports = []
try:
    for index, device in enumerate(("CPU", ch.default_device())):
        checks = []
        glass.node("surface_shader").parm("roughness").set(0)
        glass.node("surface_shader").parmTuple("color").set((1, 1, 1))
        config = profile("ACEScg", f"alpha-source-{index}")
        processor = config.getProcessor(
            "ACEScg", "Native Alpha", "Coverage", OCIO.TRANSFORM_DIR_FORWARD
        ).getDefaultCPUProcessor()
        reference = destination / f"compositing-reference-{index}.json"
        reference_env = dict(os.environ, OCIO=str(destination / f"alpha-source-{index}.ocio"))
        subprocess.run(
            [
                str(runtime_executable("ch_harness.exe")),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                device,
                "--compositing-test",
                "--output",
                str(reference),
            ],
            check=True,
            # A fresh OptiX process may need to compile its kernels first.
            timeout=300,
            env=reference_env,
        )
        reference_cases = {case["name"]: case["rgba"] for case in json.loads(reference.read_text())["cases"]}
        obj.parm("shop_materialpath").set(builder.path())
        builder.node("surface_output").setNamedInput("surface", emit, "emission")
        with ch.Session(device=device, width=96, height=64, samples=1024, collect_diagnostics=True) as session:
            frame = session.wait(session.apply(scene()))
            pixels = rgba(frame)
            np.testing.assert_allclose(pixels[32, 12], [*emitted, 1], rtol=0, atol=2e-5)
            np.testing.assert_allclose(pixels[32, 84], [*background, 1], rtol=0, atol=2e-5)
            before = session.status()
            builds = session.diagnostics()["background_builds"]
            for value in (-0.1, 1.1, float("nan"), float("inf")):
                try:
                    session.apply(dict(transparent_roughness_threshold=value))
                except RuntimeError as error:
                    assert "finite and in [0,1]" in str(error), error
                else:
                    raise AssertionError("Invalid transparent roughness threshold was accepted")
                assert session.status()["generation"] == before["generation"]
            frame = session.wait(session.apply(dict(transparent_background=True)))
            pixels = rgba(frame)
            np.testing.assert_array_equal(pixels[32, 84], np.zeros(4, np.float32))
            np.testing.assert_allclose(pixels[32, 12], [*emitted, 1], rtol=0, atol=2e-5)
            assert session.status()["geometry_conversions"] == before["geometry_conversions"]
            assert session.diagnostics()["background_builds"] == builds
            fractional = (pixels[..., 3] > 0.01) & (pixels[..., 3] < 0.99)
            assert fractional.any(), "Pixel filtering must exercise fractional coverage"
            np.testing.assert_allclose(pixels[..., :3], pixels[..., 3, None] * emitted, rtol=0, atol=2e-5)
            checks.append(check_display(session, frame, processor, "Filtered premultiplied surface coverage"))
            checks.append(
                dict(
                    name="Exact associated float32 EXR", output=check_output(session, frame, f"alpha-filtered-{index}")
                )
            )

            obj.parm("shop_materialpath").set(diffuse.path())
            transparent_frame = session.wait(session.apply(scene()))
            opaque_frame = session.wait(session.apply(dict(transparent_background=False)))
            expected = np.asarray(background) * 0.5
            np.testing.assert_allclose(rgba(transparent_frame)[32, 12, :3], expected, rtol=0, atol=0.004)
            np.testing.assert_allclose(rgba(opaque_frame)[32, 12, :3], expected, rtol=0, atol=0.004)
            checks.append(
                dict(
                    name="Transparent film preserves independently specified diffuse environment illumination",
                    expected=expected.tolist(),
                )
            )
            session.wait(session.apply(dict(transparent_background=True)))
            obj.parm("shop_materialpath").set(builder.path())

            builder.node("surface_output").setNamedInput("surface", mixed, "closure")
            frame = session.wait(session.apply(scene()))
            pixels = rgba(frame)
            assert abs(float(pixels[32, 12, 3]) - 0.625) < 0.05, pixels[32, 12]
            assert np.allclose(pixels[32, 12, :3], emitted * 0.625, atol=0.03), pixels[32, 12]
            checks.append(check_display(session, frame, processor, "Transparent closure mixed with emission"))

            obj.parm("shop_materialpath").set(glass.path())
            frame = session.wait(session.apply(scene()))
            assert rgba(frame)[32, 12, 3] > 0.99
            np.testing.assert_allclose(rgba(frame)[32, 12], reference_cases["glass_opaque"], rtol=0, atol=0.005)
            frame = session.wait(session.apply(dict(transparent_glass=True, transparent_roughness_threshold=0.1)))
            assert abs(float(rgba(frame)[32, 12, 3]) - 0.04) < 0.03, rgba(frame)[32, 12]
            np.testing.assert_allclose(rgba(frame)[32, 12], reference_cases["glass_white"], rtol=0, atol=0.005)
            checks.append(
                dict(
                    name="Smooth glass alpha matches normal-incidence Fresnel reflectance",
                    alpha=float(rgba(frame)[32, 12, 3]),
                )
            )
            glass.node("surface_shader").parmTuple("color").set((0.8, 0.8, 0.8))
            frame = session.wait(session.apply(scene()))
            np.testing.assert_allclose(rgba(frame)[32, 12], reference_cases["glass_tinted"], rtol=0, atol=0.005)
            assert abs(float(rgba(frame)[32, 12, 3]) - (1 - 0.96 * 0.8)) < 0.03
            checks.append(dict(name="Tinted glass retains attenuation in alpha", alpha=float(rgba(frame)[32, 12, 3])))
            glass.node("surface_shader").parmTuple("color").set((1, 1, 1))
            glass.node("surface_shader").parm("roughness").set(0.5)
            frame = session.wait(session.apply(scene()))
            assert rgba(frame)[32, 12, 3] > 0.99
            np.testing.assert_allclose(rgba(frame)[32, 12], reference_cases["glass_rough"], rtol=0, atol=0.005)
            checks.append(dict(name="Glass above the transparent roughness threshold remains opaque"))

            obj.parm("shop_materialpath").set(holdout.path())
            frame = session.wait(session.apply(scene()))
            np.testing.assert_array_equal(rgba(frame)[32, 12], np.zeros(4, np.float32))
            np.testing.assert_allclose(rgba(frame)[32, 12], reference_cases["holdout"], rtol=0, atol=1e-6)
            checks.append(dict(name="Holdout creates a zero-alpha, zero-radiance matte"))

            # Nine unit-density voxels at spacing 1/8: L = emission * 9/8.
            # The explicit field mask excludes renderer bounding-box padding;
            # extinction remains zero, independently of background coverage.
            frame = session.wait(session.apply(scene(volume, volume_obj)))
            glow = rgba(frame)[32, 48]
            np.testing.assert_allclose(glow[:3], np.asarray((0.5, 0.25, 0.125)) * 9 / 8, rtol=0, atol=0.01)
            assert glow[3] == 0, glow
            checks.append(check_display(session, frame, processor, "Positive emission survives zero coverage"))
            checks.append(
                dict(
                    name="Zero-alpha EXR preserves additive radiance",
                    output=check_output(session, frame, f"alpha-glow-{index}"),
                )
            )
            target = profile("Linear Rec.709 (sRGB)", f"alpha-target-{index}")
            converted = OCIO.Config.GetProcessorFromConfigs(
                config, "ACEScg", target, "Native Alpha", "Coverage", OCIO.TRANSFORM_DIR_FORWARD
            ).getDefaultCPUProcessor()
            checks.append(
                check_display(session, frame, converted, "Old zero-alpha frame across a working-space change")
            )
        assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
        reports.append(dict(device=device, checks=checks))

    rop = hou.node("/out").createNode("cycles_ipr", "native_alpha_cycles")
    set_rop_devices(rop, "CPU")
    for name, value in dict(
        soppath=source.path(),
        camera=camera.path(),
        material_kind="emission",
        resx=96,
        resy=64,
        samples=128,
        transparent_background=1,
        transparent_glass=0,
    ).items():
        rop.parm(name).set(value)
    rop.parmTuple("lightstrength").set((0, 0, 0))
    obj.parm("shop_materialpath").set(builder.path())
    builder.node("surface_output").setNamedInput("surface", emit, "emission")
    rop.parm("picture").set(str(destination / "example.$F4.exr"))
    example = destination / "native-alpha.hiplc"
    hou.hipFile.save(str(example))
    report = dict(
        result="passed",
        devices=reports,
        example=str(example),
        scope="Native transparent film, authored closures, filtered associated output and zero-alpha emission",
    )
    (destination / "houdini-transparency-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
finally:
    if original_env is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_env)
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
