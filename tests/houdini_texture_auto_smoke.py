# SPDX-License-Identifier: Apache-2.0
"""Native Auto inputs compared with independent OCIO transforms and fixed texels."""

from support.paths import artifacts_root

from array import array
import json
import os
from pathlib import Path
import sys
import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
mode = int(sys.argv[1]) if len(sys.argv) > 1 else 4
original_mode = hou.getenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
original_env = hou.getenv("OCIO")
hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", str(mode))
destination = artifacts_root() / f"stage3/color/auto/mode-{mode}"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
serial = 0
rec709 = "Linear Rec.709 (sRGB)"
srgb = "sRGB Encoded Rec.709 (sRGB)"


def profile(studio=None, rule=False, inactive=False):
    global serial
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    config.setRole(OCIO.ROLE_SCENE_LINEAR, "ACEScg")
    if studio is not None:
        space = OCIO.ColorSpace(name="Studio Input")
        space.setTransform(
            OCIO.MatrixTransform(matrix=[studio, 0, 0, 0, 0, studio, 0, 0, 0, 0, studio, 0, 0, 0, 0, 1]),
            OCIO.COLORSPACE_DIR_TO_REFERENCE,
        )
        config.addColorSpace(space)
        if inactive:
            config.setInactiveColorSpaces("Studio Input")
    if rule:
        rules = config.getFileRules()
        rules.insertRule(0, "Studio EXR", "Studio Input", "*", "exr")
        config.setFileRules(rules)
    config.validate()
    serial += 1
    path = destination / f"profile-{serial}.ocio"
    path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(path))
    hou.Color.reloadOCIO()
    return OCIO.GetCurrentConfig()


def write(path, color=(0.75, 0.375, 0.09375), storage=OIIO.FLOAT, metadata=None):
    spec = OIIO.ImageSpec(256, 256, 3, storage)
    for key, value in (metadata or {}).items():
        if key == "chromaticities":
            spec.attribute(key, OIIO.TypeDesc("float[8]"), value)
        else:
            spec.attribute(key, value)
    pixels = np.broadcast_to(np.array(color, np.float32), (256, 256, 3)).copy()
    if storage == OIIO.UINT8:
        pixels = np.round(pixels * 255).astype(np.uint8)
    output = OIIO.ImageOutput.create(str(path))
    assert output.open(str(path), spec)
    assert output.write_image(pixels) and output.close()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


geo = hou.node("/obj").createNode("geo", "auto_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
uv = geo.createNode("attribwrangle")
uv.setInput(0, grid)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "auto_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
request = ch.geometry_request(geo, uv, material="surface")
builder = ch.create_material(hou.node("/mat"), "auto_menu_material", shader="emission")
menu_texture = builder.createNode("cycles::image_texture::1", "image")
next(node for node in builder.children() if node.type().name() == "cycles::emission::1").setNamedInput(
    "color", menu_texture, "color"
)


def node(id, type, **parameters):
    return dict(id=id, definition=f"cycles/{type}/1", parameters=parameters)


def end(id, socket):
    return dict(node=id, socket=socket)


def material(path, space="auto"):
    return dict(
        key="surface",
        kind="graph",
        graph=dict(
            schema_version=1,
            nodes=[
                node(
                    "file",
                    "image_texture",
                    filename=str(path),
                    colorspace=space,
                    interpolation="closest",
                    alpha_type="channel_packed",
                ),
                node("light", "emission", strength=1.0),
            ],
            links=[{"from": end("file", "color"), "to": end("light", "color")}],
            surface=end("light", "emission"),
        ),
    )


def initial(value):
    return dict(geometry=[request], materials=[value], camera_path=camera.path(), background=(0, 0, 0))


def center(frame):
    pixels = array("f")
    pixels.frombytes(frame["planes"]["combined"]["pixels"])
    return np.array(pixels)[(16 * 32 + 16) * 4 : (16 * 32 + 16) * 4 + 3]


def texel(frame, x):
    pixels = array("f")
    pixels.frombytes(frame["planes"]["combined"]["pixels"])
    return np.array(pixels)[(16 * 32 + x) * 4 : (16 * 32 + x) * 4 + 3]


def expect(frame, config, space, pixel=(0.75, 0.375, 0.09375)):
    expected = (
        pixel
        if space in ("data", "scene_linear")
        else config.getProcessor(space, OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB(list(pixel))
    )
    actual = center(frame)
    assert np.allclose(actual, expected, rtol=0, atol=1e-4), (actual, expected, space)
    return dict(measured=actual.tolist(), expected=list(expected))


def check_source_diagnostic(session, value, message):
    # Invalid external image inputs use recoverable diagnostic shading. They
    # must never silently choose an input transform or disturb owned geometry.
    before = session.status()
    frame = session.wait(session.apply(dict(materials=[value])))
    status = session.status()
    assert status["state"] != "failed" and not status["error"], status
    assert status["generation"] > before["generation"], status
    assert status["geometry_conversions"] == before["geometry_conversions"]
    assert status["extractions"] == before["extractions"]
    assert np.allclose(center(frame), (1, 0, 1), rtol=0, atol=1e-5), center(frame)
    issues = status["source_diagnostics"]
    assert len(issues) == 1 and issues[0]["code"] == "texture.unavailable", issues
    assert message in issues[0]["message"], (message, issues)
    assert (
        Path(issues[0]["source"]).resolve() == Path(value["graph"]["nodes"][0]["parameters"]["filename"]).resolve()
    ), issues


def reject(session, value, message):
    # Host policy failures remain transaction errors, unlike recoverable
    # texture-source errors raised by the core image validation above.
    generation = session.status()["generation"]
    frame = session.frame_info()
    inputs = session.texture_inputs()
    try:
        session.apply(dict(materials=[value]))
    except RuntimeError as error:
        assert message in str(error), (message, str(error))
    else:
        raise AssertionError("Invalid Auto update succeeded: " + message + ": " + json.dumps(session.texture_inputs()))
    assert (
        session.status()["generation"] == generation
        and session.frame_info() == frame
        and session.texture_inputs() == inputs
    )


cases = [
    ("ordinary.exr", OIIO.FLOAT, {}, rec709, (0.75, 0.375, 0.09375)),
    ("shot_ACEScg.exr", OIIO.FLOAT, {}, rec709 if mode == 0 else "ACEScg", (0.75, 0.375, 0.09375)),
    ("ordinary.png", OIIO.UINT8, {}, srgb, (191 / 255, 96 / 255, 24 / 255)),
    ("shot_ACEScg.png", OIIO.UINT8, {}, srgb if mode == 0 else "ACEScg", (191 / 255, 96 / 255, 24 / 255)),
    ("ordinary.hdr", OIIO.FLOAT, {}, "ACES2065-1" if mode in (1, 2) else rec709, (0.75, 0.375, 0.09375)),
    ("metadata.exr", OIIO.FLOAT, {"oiio:ColorSpace": srgb}, srgb if mode in (0, 3) else rec709, (0.75, 0.375, 0.09375)),
    (
        "conflict_ACEScg.exr",
        OIIO.FLOAT,
        {"oiio:ColorSpace": rec709},
        rec709 if mode == 0 else "ACEScg",
        (0.75, 0.375, 0.09375),
    ),
    ("shot-lin_ap1.exr", OIIO.FLOAT, {}, "ACEScg" if mode == 3 else rec709, (0.75, 0.375, 0.09375)),
    ("shot-RAW.exr", OIIO.FLOAT, {}, "Raw" if mode == 3 else rec709, (0.75, 0.375, 0.09375)),
]
for name, storage, metadata, space, pixel in cases:
    write(destination / name, storage=storage, metadata=metadata)

reports = []
try:
    for device in ("CPU", ch.default_device()):
        for cache in (False, True):
            hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", str(mode))
            config = profile()
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
                for index, (name, storage, metadata, space, pixel) in enumerate(cases):
                    value = material(destination / name)
                    frame = session.wait(session.apply(initial(value) if index == 0 else dict(materials=[value])))
                    inputs = session.texture_inputs()
                    assert len(inputs) == 1
                    assert inputs[0]["colorspace"] == config.getColorSpace(space).getName(), inputs
                    assert f"mode={mode}:" in inputs[0]["policy_id"], inputs
                    checks.append(dict(name=name, space=space, input=inputs[0], **expect(frame, config, space, pixel)))
                session.wait(session.apply(dict(materials=[material(destination / "conflict_ACEScg.exr")])))
                conversions = session.status()["geometry_conversions"]
                extractions = session.status()["extractions"]
                builds = session.diagnostics()["material_builds"]
                before = session.texture_inputs()
                session.wait(session.apply(dict(camera_path=camera.path())))
                assert session.texture_inputs() == before and session.diagnostics()["material_builds"] == builds
                assert (
                    session.status()["geometry_conversions"] == conversions
                    and session.status()["extractions"] == extractions
                )
                checks.append(dict(name="camera-only update retains image/shader resources", inputs=before))

                # Change the host filename policy while retaining the original
                # Auto graph and files. The node graph must not become a named input.
                alternative = 4 if mode == 0 else 0
                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", str(alternative))
                assert session.textures_changed()
                frame = session.wait(session.apply({}))
                space = "ACEScg" if alternative == 4 else rec709
                checks.append(
                    dict(name="retained Auto graph follows filename policy edits", **expect(frame, config, space))
                )
                assert (
                    session.status()["geometry_conversions"] == conversions
                    and session.status()["extractions"] == extractions
                )
                assert not session.textures_changed()
                frame = session.wait(
                    session.apply(dict(materials=[material(destination / "conflict_ACEScg.exr", "data")]))
                )
                expect(frame, config, "data")
                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", str(mode))
                assert not session.textures_changed()
                checks.append(dict(name="explicit Data ignores filename-policy changes"))
                frame = session.wait(
                    session.apply(dict(materials=[material(destination / "conflict_ACEScg.exr", "scene_linear")]))
                )
                expect(frame, config, "scene_linear")
                checks.append(dict(name="explicit working space bypasses metadata and filename"))

                # A filename-rule reload and same-name input-transform edit
                # exercise committed, owned Auto semantics via apply({}).
                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "4")
                session.wait(session.apply(dict(materials=[material(destination / "ordinary.exr")])))
                config = profile(0.5, True)
                assert ch.synchronize_color_configuration() and session.textures_changed()
                assert "Studio Input" in menu_texture.parm("colorspace").menuItems()
                frame = session.wait(session.apply({}))
                checks.append(
                    dict(name="file-rule reload re-resolves retained Auto", **expect(frame, config, "Studio Input"))
                )
                revision = session.texture_inputs()[0]["revision"]
                config = profile(0.25, True)
                assert ch.synchronize_color_configuration()
                frame = session.wait(session.apply({}))
                checks.append(
                    dict(name="same-name Auto input transform reload", **expect(frame, config, "Studio Input"))
                )
                assert session.texture_inputs()[0]["revision"] != revision
                assert (
                    session.status()["geometry_conversions"] == conversions
                    and session.status()["extractions"] == extractions
                )
                menu_texture.parm("colorspace").set("Studio Input")
                generation = session.status()["generation"]
                config = profile(0.25, True, inactive=True)
                assert not ch.synchronize_color_configuration()
                assert "Studio Input" not in menu_texture.parm("colorspace").menuItems()
                assert menu_texture.evalParm("colorspace") == "Studio Input" and not session.textures_changed()
                assert session.status()["generation"] == generation
                authored = ch.material_graph(builder)
                assert (
                    next(node for node in authored["nodes"] if node["definition"] == "cycles/image_texture/1")[
                        "parameters"
                    ]["colorspace"]
                    == "Studio Input"
                )
                checks.append(
                    dict(
                        name="live OCIO input menu hides inactive spaces without changing saved names or the current render"
                    )
                )
                if cache:
                    assert list(destination.rglob("*.tx")), "No upstream texture cache derivatives"

                hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "3")
                config = profile()
                ch.synchronize_color_configuration()
                session.wait(session.apply(dict(materials=[material(destination / "ordinary.exr", "data")])))
                write(destination / "tiles.1001.exr", metadata={"oiio:ColorSpace": "ACEScg"})
                write(destination / "tiles.1002.exr", metadata={"oiio:ColorSpace": rec709})
                check_source_diagnostic(
                    session, material(destination / "tiles.<UDIM>.exr"), "different input colorspaces"
                )
                checks.append(
                    dict(name="mixed-space UDIM Auto renders a visible source diagnostic without guessing a transform")
                )
                session.wait(session.apply(dict(materials=[material(destination / "tiles.<UDIM>.exr", "data")])))
                expect(session.frame(), config, "data")
                assert not session.status()["source_diagnostics"], session.status()
                assert session.texture_inputs()[0]["tiles"] == [1001, 1002]
                checks.append(dict(name="explicit Data permits differently tagged UDIM tiles"))
                config = profile(0.5)
                ch.synchronize_color_configuration()
                uv.parm("snippet").set("v@uv=set(@P.x/4+1,@P.y/8+0.5,0);")
                write(destination / "uniform.1001.exr", metadata={"oiio:ColorSpace": "Studio Input"})
                second_rgb = [0.1875, 0.375, 0.75]
                write(destination / "uniform.1002.exr", second_rgb, metadata={"oiio:ColorSpace": "Studio Input"})
                frame = session.wait(
                    session.apply(dict(geometry=[request], materials=[material(destination / "uniform.<UDIM>.exr")]))
                )

                def check_tiles(frame, config):
                    processor = config.getProcessor("Studio Input", OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor()
                    first = processor.applyRGB([0.75, 0.375, 0.09375])
                    second = processor.applyRGB(second_rgb)
                    assert np.allclose(texel(frame, 8), first, rtol=0, atol=1e-4) and np.allclose(
                        texel(frame, 24), second, rtol=0, atol=1e-4
                    )
                    return dict(
                        first=texel(frame, 8).tolist(),
                        second=texel(frame, 24).tolist(),
                        expected_first=first,
                        expected_second=second,
                    )

                checks.append(
                    dict(
                        name="uniform Auto UDIMs use independently transformed texels on both tiles",
                        **check_tiles(frame, config),
                    )
                )
                conversions = session.status()["geometry_conversions"]
                extractions = session.status()["extractions"]
                config = profile(0.25)
                assert ch.synchronize_color_configuration()
                frame = session.wait(session.apply({}))
                checks.append(
                    dict(
                        name="Auto UDIM transform reload updates both tiles without geometry extraction",
                        **check_tiles(frame, config),
                    )
                )
                assert (
                    session.status()["geometry_conversions"] == conversions
                    and session.status()["extractions"] == extractions
                )
                uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
                write(destination / "unknown.exr", metadata={"oiio:ColorSpace": "Missing Studio Space"})
                check_source_diagnostic(session, material(destination / "unknown.exr"), "Unknown texture colorspace")
                write(
                    destination / "primaries.exr",
                    metadata={"chromaticities": (0.713, 0.293, 0.165, 0.830, 0.128, 0.044, 0.32168, 0.33767)},
                )
                frame = session.wait(session.apply(dict(materials=[material(destination / "primaries.exr")])))
                assert not session.status()["source_diagnostics"], session.status()
                checks.append(
                    dict(name="ACEScg legacy primaries resolve independently", **expect(frame, config, "ACEScg"))
                )
                write(
                    destination / "unsupported-primaries.exr",
                    metadata={"chromaticities": (0.713, 0.293, 0.165, 0.830, 0.11, 0.044, 0.32168, 0.33767)},
                )
                reject(
                    session, material(destination / "unsupported-primaries.exr"), "unsupported texture chromaticities"
                )
                checks.append(
                    dict(name="unknown named inputs render a diagnostic; unsupported host primaries roll back")
                )
                hou.putenv("OCIO", "")
                hou.Color.reloadOCIO()
                reject(session, material(destination / "ordinary.exr"), "valid active Houdini OCIO configuration")
                checks.append(
                    dict(name="disabled host OCIO rejects Auto without silently using the retained global config")
                )
                reports.append(dict(device=device, cache=cache, mode=mode, result="passed", checks=checks))
            assert session.status()["device_allocation_bytes"] == 0
    report = dict(
        result="passed",
        mode=mode,
        cases=reports,
        scope="Documented native filename/metadata/format policy and independently transformed texels, including ACEScg legacy primaries. Complete portable-header conversion has its own independent suite.",
    )
    (destination / "houdini-texture-auto-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    # The aggregate runner consumes the main/default mode report.
    if mode == 4:
        (destination.parent / "houdini-texture-auto-smoke.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    print(json.dumps(report))
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
