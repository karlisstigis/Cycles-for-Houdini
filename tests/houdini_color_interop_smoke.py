# SPDX-License-Identifier: Apache-2.0
"""Portable headers and EXR identity against independent OCIO configurations."""

from support.paths import artifacts_root
from array import array
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/portable"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_env = hou.getenv("OCIO")
original_mode = hou.getenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
builtin = OCIO.Config.CreateFromBuiltinConfig("studio-config-latest")
serial = 0
pixel = [0.75, 0.375, 0.09375]
standards = {
    "lin_ap1_scene": ("ACEScg", (0.713, 0.293, 0.165, 0.830, 0.128, 0.044, 0.32168, 0.33767)),
    "lin_ap0_scene": ("ACES2065-1", (0.7347, 0.2653, 0, 1, 0.0001, -0.0770, 0.32168, 0.33767)),
    "lin_rec709_scene": ("Linear Rec.709 (sRGB)", (0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.3290)),
    "lin_rec2020_scene": ("Linear Rec.2020", (0.708, 0.292, 0.170, 0.797, 0.131, 0.046, 0.3127, 0.3290)),
    "lin_p3d65_scene": ("Linear P3-D65", (0.680, 0.320, 0.265, 0.690, 0.150, 0.060, 0.3127, 0.3290)),
    "lin_adobergb_scene": ("Linear AdobeRGB", (0.640, 0.330, 0.210, 0.710, 0.150, 0.060, 0.3127, 0.3290)),
}


def activate(config, label):
    global serial
    config.validate()
    serial += 1
    path = destination / f"{serial}-{label}.ocio"
    path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(path))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
    return OCIO.GetCurrentConfig()


def studio(name="Studio Show", scale=1.0, nonlinear=False, authored="", interchange=True):
    # Independently defined spaces, without any of the standard aliases.
    config = OCIO.Config()
    config.setVersion(2, 5 if interchange else 1)
    config.setName(name)
    reference = OCIO.ColorSpace(name="Pipeline Reference")
    reference.addAlias("studio:reference")
    config.addColorSpace(reference)
    transform = original.getProcessor("ACEScg", "aces_interchange").createGroupTransform()
    if scale != 1:
        transform.appendTransform(
            OCIO.MatrixTransform(matrix=[scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, 1])
        )
    if nonlinear:
        transform.appendTransform(OCIO.ExponentTransform(value=[2, 2, 2, 1]))
    working = OCIO.ColorSpace(name="Studio Working")
    working.setTransform(transform, OCIO.COLORSPACE_DIR_TO_REFERENCE)
    if authored:
        working.setInteropID(authored)
        working.addAlias(authored)
    config.addColorSpace(working)
    raw = OCIO.ColorSpace(name="Studio Raw", isData=True)
    config.addColorSpace(raw)
    config.setRole(OCIO.ROLE_SCENE_LINEAR, working.getName())
    config.setRole(OCIO.ROLE_DEFAULT, working.getName())
    config.setRole(OCIO.ROLE_DATA, raw.getName())
    log = OCIO.ColorSpace(name="Pipeline Log")
    log.setTransform(
        original.getProcessor("ACEScct", "aces_interchange").createGroupTransform(), OCIO.COLORSPACE_DIR_TO_REFERENCE
    )
    config.addColorSpace(log)
    config.setRole(OCIO.ROLE_COMPOSITING_LOG, log.getName())
    config.setRole(OCIO.ROLE_COLOR_TIMING, log.getName())
    if interchange:
        config.setRole("aces_interchange", reference.getName())
    config.addDisplayView("Studio Display", "Raw", working.getName(), "")
    return config


def write(path, metadata, storage=OIIO.FLOAT, source_pixel=None):
    spec = OIIO.ImageSpec(256, 256, 3, storage)
    for key, value in metadata.items():
        if key == "chromaticities":
            spec.attribute(key, OIIO.TypeDesc("float[8]"), value)
        else:
            spec.attribute(key, value)
    output = OIIO.ImageOutput.create(str(path))
    assert output.open(str(path), spec)
    pixels = np.broadcast_to(
        np.array(pixel if source_pixel is None else source_pixel, np.float32), (256, 256, 3)
    ).copy()
    if storage == OIIO.UINT8:
        pixels = np.round(pixels * 255).astype(np.uint8)
    assert output.write_image(pixels)
    assert output.close()
    return path


geo = hou.node("/obj").createNode("geo", "interop_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
uv = geo.createNode("attribwrangle")
uv.setInput(0, grid)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "interop_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
request = ch.geometry_request(geo, uv, material="surface")
builder = ch.create_material(hou.node("/mat"), "interop_menu_material", shader="emission")
menu_texture = builder.createNode("cycles::image_texture::1", "image")


def material(path, space="auto"):
    return dict(
        key="surface",
        kind="graph",
        graph=dict(
            schema_version=1,
            nodes=[
                dict(
                    id="file",
                    definition="cycles/image_texture/1",
                    parameters=dict(
                        filename=str(path), colorspace=space, interpolation="closest", alpha_type="channel_packed"
                    ),
                ),
                dict(id="light", definition="cycles/emission/1", parameters=dict(strength=1.0)),
            ],
            links=[{"from": dict(node="file", socket="color"), "to": dict(node="light", socket="color")}],
            surface=dict(node="light", socket="emission"),
        ),
    )


def image(frame):
    floats = array("f")
    floats.frombytes(frame["planes"]["combined"]["pixels"])
    return np.asarray(floats).reshape((frame["height"], frame["width"], 4))


def render_input(
    session, path, config, source_config, source_name, label, expected_selection=None, space="auto", source_pixel=None
):
    texel = pixel if source_pixel is None else source_pixel
    expected = (
        texel
        if source_name == "data"
        else OCIO.Config.GetProcessorFromConfigs(source_config, source_name, config, OCIO.ROLE_SCENE_LINEAR)
        .getDefaultCPUProcessor()
        .applyRGB(texel)
    )
    before = session.status()
    frame = session.wait(session.apply(dict(materials=[material(path, space)])))
    actual = image(frame)[16, 16, :3]
    assert np.isfinite(actual).all() and np.allclose(actual, expected, rtol=0, atol=1e-4), (label, actual, expected)
    assert session.status()["geometry_conversions"] == before["geometry_conversions"]
    inputs = session.texture_inputs()
    if expected_selection:
        assert inputs[0]["selection"] == expected_selection, inputs
    assert not inputs[0]["colorspace"].startswith("__cycles_houdini_portable_"), inputs
    return dict(name=label, measured=actual.tolist(), expected=list(expected), inputs=inputs)


def reject(session, path, message):
    before = session.status()
    inputs = session.texture_inputs()
    source = session.frame_info()
    try:
        session.apply(dict(materials=[material(path)]))
    except RuntimeError as error:
        assert message in str(error), (message, str(error))
    else:
        raise AssertionError("Expected a nonmutating invalid input failure")
    assert session.status()["generation"] == before["generation"]
    assert session.frame_info() == source and session.texture_inputs() == inputs


def clean_cache(session, path, expected_id):
    info = session.texture_inputs()[0]
    revision = info["revision"]
    assert info["portable_decode_bytes"] == 256 * 256 * 16, info
    assert info["portable_cache_format"] == "float32 RGBA OpenEXR, 64x64 tiles, mipmaps", info
    files = list((path.parent / "cycles_houdini" / revision).glob(path.name + ".*.tx"))
    assert len(files) == 1, (path, files, revision)
    reader = OIIO.ImageInput.open(str(files[0]))
    assert reader
    attributes = {value.name: value.value for value in reader.spec().extra_attribs}
    assert reader.spec().format == OIIO.FLOAT and reader.spec().nchannels == 4
    assert reader.spec().tile_width == reader.spec().tile_height == 64
    reader.close()
    assert attributes.get("colorInteropID") == expected_id, (path, attributes)
    assert "acesImageContainer" not in attributes and "chromaticities" not in attributes, (path, attributes)
    return dict(name="Derived cache replaces source color declarations", file=str(files[0]), id=expected_id)


def portable_alpha(session, config):
    expected_rgb = (
        OCIO.Config.GetProcessorFromConfigs(builtin, "ACES2065-1", config, OCIO.ROLE_SCENE_LINEAR)
        .getDefaultCPUProcessor()
        .applyRGB(pixel)
    )
    checks = []
    alpha = 0.375
    for mode in ("associated", "unassociated", "channel_packed"):
        path = destination / ("portable-alpha-" + mode + ".exr")
        spec = OIIO.ImageSpec(256, 256, 4, OIIO.FLOAT)
        spec.alpha_channel = 3
        spec.attribute("colorInteropID", "lin_ap0_scene")
        spec.attribute("oiio:UnassociatedAlpha", int(mode == "unassociated"))
        payload = [v * alpha for v in pixel] if mode == "associated" else pixel
        writer = OIIO.ImageOutput.create(str(path))
        assert writer.open(str(path), spec)
        assert writer.write_image(np.broadcast_to(np.asarray([*payload, alpha], np.float32), (256, 256, 4)).copy())
        assert writer.close()
        for output in ("alpha", "color_with_alpha", "color_only"):
            graph = material(path)
            shader = graph["graph"]
            shader["nodes"][0]["parameters"]["alpha_type"] = mode
            if output == "alpha":
                shader["nodes"].append(dict(id="alpha_rgb", definition="cycles/combine_xyz/1", parameters={}))
                shader["links"] = [
                    {"from": dict(node="file", socket="alpha"), "to": dict(node="alpha_rgb", socket=axis)}
                    for axis in ("x", "y", "z")
                ] + [{"from": dict(node="alpha_rgb", socket="vector"), "to": dict(node="light", socket="color")}]
                expected = [alpha] * 3
            elif output == "color_with_alpha":
                shader["links"].append(
                    {"from": dict(node="file", socket="alpha"), "to": dict(node="light", socket="strength")}
                )
                expected = [v * alpha for v in expected_rgb]
            else:
                expected = expected_rgb if mode == "channel_packed" else [v * alpha for v in expected_rgb]
            actual = image(session.wait(session.apply(dict(materials=[graph]))))[16, 16, :3]
            assert np.allclose(actual, expected, rtol=0, atol=1e-4), (mode, output, actual, expected)
            checks.append(dict(mode=mode, output=output, measured=actual.tolist(), expected=expected))
    return dict(name="Portable fallback preserves associated/unassociated/channel-packed RGB and alpha", checks=checks)


def portable_udim(session, config):
    second = [0.1875, 0.375, 0.75]
    write(destination / "portable-tiles.1001.exr", {"colorInteropID": "lin_ap0_scene"})
    write(destination / "portable-tiles.1002.exr", {"colorInteropID": "lin_ap0_scene"}, source_pixel=second)
    uv.parm("snippet").set("v@uv=set(@P.x/4+1,@P.y/8+0.5,0);")
    try:
        frame = session.wait(
            session.apply(dict(geometry=[request], materials=[material(destination / "portable-tiles.<UDIM>.exr")]))
        )
        processor = OCIO.Config.GetProcessorFromConfigs(
            builtin, "ACES2065-1", config, OCIO.ROLE_SCENE_LINEAR
        ).getDefaultCPUProcessor()
        expected = [processor.applyRGB(value) for value in (pixel, second)]
        actual = [image(frame)[16, column, :3].tolist() for column in (8, 24)]
        assert np.allclose(actual, expected, rtol=0, atol=1e-4), (actual, expected)
        assert session.texture_inputs()[0]["tiles"] == [1001, 1002]
        return dict(name="Portable fallback independently converts both UDIM tiles", measured=actual, expected=expected)
    finally:
        uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
        session.wait(session.apply(dict(geometry=[request])))


def output(session, frame, name, expected_id):
    path = destination / (name + ".exr")
    session.write_exr(path)
    reader = OIIO.ImageInput.open(str(path))
    assert reader
    spec = reader.spec()
    actual_id = spec.get_string_attribute("colorInteropID")
    assert actual_id == expected_id, (name, actual_id, expected_id)
    assert spec.get_string_attribute("oiio:ColorSpace") == frame["working_space"]
    attributes = {value.name: value.value for value in spec.extra_attribs}
    assert "chromaticities" not in attributes and "acesImageContainer" not in attributes, (name, attributes)
    pixels = reader.read_image(OIIO.FLOAT)
    rgba = pixels[:, :, [list(spec.channelnames).index(c) for c in "RGBA"]]
    assert np.array_equal(rgba, image(frame)[::-1]), name
    reader.close()
    return dict(name=name, id=actual_id, working=frame["working_space"], exact_float32_pixels=True)


reports = []
try:
    hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "0")
    for device_index, device in enumerate(("CPU", ch.default_device())):
        # Output identities are tested independently from texture reading.
        outputs = []
        for id, (working, _) in standards.items():
            config = OCIO.Config.CreateFromStream(original.serialize())
            config.setWorkingDir(original.getWorkingDir())
            config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
            activate(config, "standard-output")
            with ch.Session(device=device, width=16, height=16, samples=4) as session:
                frame = session.wait(session.calibration())
                assert ch.color_policy()["output_interop_id"] == id, ch.color_policy()
                outputs.append(output(session, frame, f"output-{device_index}-{id}", id))
        for config, expected_id, label in (
            (studio(), "lin_ap1_scene", "Renamed equivalent working space"),
            (studio(scale=0.8), "studio_show:local:studio_working", "Scaled custom working space"),
            (studio(nonlinear=True), "studio_show:local:studio_working", "Nonlinear working space"),
            (studio(name=""), "unknown", "Unnamed config has no local identity"),
            (
                studio(name="Show {A}: Démo", scale=0.8),
                "show_(a)|_d^mo:local:studio_working",
                "Sanitized local identity",
            ),
            (studio(authored="studio:working"), "studio:working", "Authored identity takes precedence"),
            (
                studio(interchange=False),
                "studio_show:local:studio_working",
                "Unestablished reference cannot imply a standard",
            ),
        ):
            # Unnamed but still mathematically equivalent spaces should be identified;
            # give this case distinct primaries so it requires a local identity.
            if label.startswith("Unnamed"):
                config = studio(name="", scale=0.8)
            activate(config, "custom-output")
            with ch.Session(device=device, width=16, height=16, samples=4) as session:
                frame = session.wait(session.calibration())
                policy = ch.color_policy()
                assert policy["output_interop_id"] == expected_id, (label, policy)
                outputs.append(output(session, frame, f"custom-output-{device_index}-{len(outputs)}", expected_id))
                if label == "Scaled custom working space":
                    activate(studio(name="Another Show", scale=0.6), "retained-output-target")
                    outputs.append(output(session, frame, f"retained-output-{device_index}", expected_id))
        # Primaries do not establish a transfer function in a PNG. Use the
        # normal host configuration for this format-default regression.
        config = OCIO.Config.CreateFromStream(original.serialize())
        config.setWorkingDir(original.getWorkingDir())
        config.setRole(OCIO.ROLE_SCENE_LINEAR, "ACEScg")
        config = activate(config, "png-format")
        path = write(
            destination / "primaries.png",
            {"oiio:ColorSpace": "sRGB", "chromaticities": standards["lin_rec709_scene"][1]},
            storage=OIIO.UINT8,
        )
        quantized = (np.round(np.asarray(pixel) * 255) / 255).tolist()
        with ch.Session(device=device, width=32, height=32, samples=8) as session:
            session.wait(
                session.apply(
                    dict(
                        geometry=[request],
                        materials=[material(path, "data")],
                        camera_path=camera.path(),
                        background=(0, 0, 0),
                    )
                )
            )
            png_check = render_input(
                session,
                path,
                config,
                builtin,
                "sRGB Encoded Rec.709 (sRGB)",
                "PNG primaries preserve encoded format interpretation",
                source_pixel=quantized,
            )
            path = write(
                destination / "legacy-srgb.exr",
                {"oiio:ColorSpace": "sRGB", "chromaticities": standards["lin_rec709_scene"][1]},
            )
            legacy_encoded_check = render_input(
                session,
                path,
                config,
                builtin,
                "sRGB Encoded Rec.709 (sRGB)",
                "Legacy EXR sRGB declaration preserves its transfer function",
            )
        for cache in (False, True):
            config = activate(studio(), "input-studio")
            definitions = config.serialize()
            checks = []
            with ch.Session(
                device=device,
                width=32,
                height=32,
                samples=8,
                collect_diagnostics=True,
                use_texture_cache=cache,
                auto_texture_cache=cache,
            ) as session:
                session.wait(
                    session.apply(
                        dict(
                            geometry=[request],
                            materials=[material(write(destination / "initial.exr", {"colorInteropID": "data"}))],
                            camera_path=camera.path(),
                            background=(0, 0, 0),
                        )
                    )
                )
                for id, (space_name, primaries) in standards.items():
                    path = write(destination / f"{id}.exr", {"colorInteropID": id})
                    checks.append(
                        render_input(session, path, config, builtin, space_name, "Portable " + id, "colorInteropID")
                    )
                    path = write(destination / f"legacy-{id}.exr", {"chromaticities": primaries})
                    checks.append(
                        render_input(session, path, config, builtin, space_name, "Legacy " + id, "chromaticities")
                    )
                for id in (
                    "srgb_rec709_scene",
                    "srgb_ap1_scene",
                    "g22_adobergb_scene",
                    "g18_rec709_scene",
                    "g22_rec709_scene",
                    "srgb_p3d65_scene",
                    "g22_ap1_scene",
                    "ocio:arrilogc4_awg4_scene",
                ):
                    path = write(destination / f"encoded-{id.replace(':', '-')}.exr", {"colorInteropID": id})
                    checks.append(
                        render_input(
                            session,
                            path,
                            config,
                            builtin,
                            builtin.getColorSpace(id).getName(),
                            "Encoded " + id,
                            "colorInteropID",
                        )
                    )
                # The July 2026 CIF catalogue has two encodings not named in the
                # installed built-in Studio catalogue. Specify their references
                # independently of the integration's compatibility definitions.
                path = write(destination / "gamma24.exr", {"colorInteropID": "g24_rec709_scene"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        builtin,
                        "Linear Rec.709 (sRGB)",
                        "Published gamma 2.4 scene ID",
                        "colorInteropID",
                        source_pixel=(np.asarray(pixel) ** 2.4).tolist(),
                    )
                )
                path = write(destination / "xyz-d65.exr", {"colorInteropID": "lin_ciexyzd65_scene"})
                # Numerical coefficients from the ASWF reference configuration:
                # https://github.com/AcademySoftwareFoundation/ColorInterop/blob/main/Recommendations/01_TextureAssetColorSpaces/core-renderer-config.ocio
                # The integration instead invokes the SDK's Bradford utility.
                xyz_to_ap0 = np.array(
                    [
                        [1.0634954914942, 0.00640891019711777, -0.0158067866176054],
                        [-0.492074127923892, 1.36822340747333, 0.0913370883144736],
                        [-0.00281646163925351, 0.00464417105680067, 0.916418574593656],
                    ]
                )
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        builtin,
                        "ACES2065-1",
                        "Published scene XYZ-D65 ID",
                        "colorInteropID",
                        source_pixel=(xyz_to_ap0 @ pixel).tolist(),
                    )
                )
                path = write(destination / "namespace.exr", {"colorInteropID": "show:lin_rec709_scene"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        builtin,
                        "Linear Rec.709 (sRGB)",
                        "One namespace fallback",
                        "colorInteropID",
                    )
                )
                path = write(destination / "exact-namespace.exr", {"colorInteropID": "show:studio:reference"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        config,
                        "Pipeline Reference",
                        "Drop only the outer namespace",
                        "colorInteropID",
                    )
                )
                path = write(destination / "data.exr", {"colorInteropID": "data", "oiio:ColorSpace": "Studio Working"})
                checks.append(
                    render_input(
                        session, path, config, config, "data", "Data wins named color metadata", "colorInteropID"
                    )
                )
                path = write(destination / "container.exr", {"acesImageContainer": 1, "colorInteropID": "data"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        builtin,
                        "ACES2065-1",
                        "ACES container wins conflicting ID",
                        "acesImageContainer",
                    )
                )
                if cache:
                    artifact = clean_cache(session, path, "lin_ap1_scene")
                    checks.append(artifact)
                    converted = (
                        OCIO.Config.GetProcessorFromConfigs(builtin, "ACES2065-1", config, OCIO.ROLE_SCENE_LINEAR)
                        .getDefaultCPUProcessor()
                        .applyRGB(pixel)
                    )
                    checks.append(
                        render_input(
                            session,
                            Path(artifact["file"]),
                            config,
                            config,
                            "Studio Working",
                            "Converted cache roundtrip uses its new identity",
                            "colorInteropID",
                            source_pixel=converted,
                        )
                    )
                    blocked = destination / ("blocked-cache-cpu" if device == "CPU" else "blocked-cache-optix")
                    blocked.mkdir(exist_ok=True)
                    (blocked / "cycles_houdini").write_text("A file intentionally blocks optional cache creation.")
                    path = write(blocked / "portable.exr", {"colorInteropID": "lin_ap0_scene"})
                    check = render_input(
                        session,
                        path,
                        config,
                        builtin,
                        "ACES2065-1",
                        "Unwritable portable cache retains source conversion",
                        "colorInteropID",
                    )
                    assert (blocked / "cycles_houdini").is_file()
                    assert not list(blocked.rglob("*.tx"))
                    check["cache_creation_blocked"] = True
                    checks.append(check)
                path = write(
                    destination / "conflict.exr",
                    {
                        "colorInteropID": "lin_ap0_scene",
                        "oiio:ColorSpace": "Studio Working",
                        "chromaticities": standards["lin_rec709_scene"][1],
                    },
                )
                checks.append(portable_alpha(session, config))
                checks.append(portable_udim(session, config))
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        builtin,
                        "ACES2065-1",
                        "Portable ID wins stale name and primaries",
                        "colorInteropID",
                    )
                )
                path = write(destination / "local.exr", {"colorInteropID": "studio_show:local:studio_working"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        config,
                        "Studio Working",
                        "Matching local configuration",
                        "colorInteropID",
                    )
                )
                path = write(destination / "foreign-local.exr", {"colorInteropID": "foreign_show:local:studio_working"})
                reject(session, path, "cannot resolve portable")
                checks.append(dict(name="Foreign local ID fails without mutating the scene"))
                path = write(destination / "two-namespace.exr", {"colorInteropID": "show:foreign:lin_rec709_scene"})
                reject(session, path, "cannot resolve portable")
                checks.append(dict(name="Two namespaces do not fall through to an unqualified base"))
                path = write(
                    destination / "unknown-metadata.exr",
                    {"colorInteropID": "unknown", "oiio:ColorSpace": "Studio Working"},
                )
                checks.append(
                    render_input(
                        session, path, config, config, "Studio Working", "Unknown ID uses reliable named metadata"
                    )
                )
                path = write(
                    destination / "unqualified-storage.exr",
                    {"colorInteropID": "unknown", "oiio:ColorSpace": "scene_linear"},
                )
                reject(session, path, "engine storage")
                checks.append(dict(name="Unqualified engine storage does not assert the current working space"))
                path = write(destination / "explicit.exr", {"colorInteropID": "foreign:missing"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        config,
                        "data",
                        "Explicit input overrides unresolved ID",
                        "explicit",
                        space="data",
                    )
                )
                assert OCIO.GetCurrentConfig().serialize() == definitions, (
                    "Portable overlay mutated the artist configuration"
                )
                assert not any(
                    name.startswith("__cycles_houdini_portable_")
                    for name in menu_texture.parm("colorspace").menuItems()
                )
                checks.append(dict(name="Private overlay does not enter the artist configuration or input menus"))
                # Pipeline/filename overrides remain authoritative over portable
                # headers. Test the host selector rather than assuming this follows
                # from generic ID resolution.
                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "3")
                path = write(destination / "forced_Studio Working.exr", {"colorInteropID": "lin_ap0_scene"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        config,
                        "Studio Working",
                        "Filename overrides portable metadata",
                        "filename",
                    )
                )
                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "0")
                path = write(destination / "retained-local.exr", {"colorInteropID": "studio_show:local:studio_working"})
                render_input(session, path, config, config, "Studio Working", "Prepare retained local input")
                config = activate(studio(name="Renamed Studio"), "renamed-local-input")
                assert session.textures_changed(), "Configuration rename did not invalidate local input identity"
                before = session.status()["generation"]
                try:
                    session.apply({})
                except RuntimeError as error:
                    assert "cannot resolve portable" in str(error), str(error)
                else:
                    raise AssertionError("A local ID from the old configuration survived a rename")
                assert session.status()["generation"] == before
                path = write(path, {"colorInteropID": "renamed_studio:local:studio_working"})
                checks.append(
                    render_input(
                        session,
                        path,
                        config,
                        config,
                        "Studio Working",
                        "Configuration rename re-resolves retained local ID",
                        "colorInteropID",
                    )
                )
            assert session.status()["device_allocation_bytes"] == 0
            reports.append(
                dict(
                    device=device,
                    cache=cache,
                    result="passed",
                    outputs=outputs,
                    png=png_check,
                    legacy_encoded=legacy_encoded_check,
                    checks=checks,
                )
            )
finally:
    if original_env is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_env)
    if original_mode is None:
        hou.unsetenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
    else:
        hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", original_mode)
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()

report = dict(
    result="passed",
    reference_builtin_configuration=builtin.getName(),
    integration_builtin_configuration=ch.color_policy()["builtin_configuration"],
    cases=reports,
    scope="Portable inputs without config mutation, source-tagged EXR, alpha conventions, UDIMs and optional cache failure; full transparency and Solaris certification remain separate",
)
(destination / "houdini-color-interop-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
