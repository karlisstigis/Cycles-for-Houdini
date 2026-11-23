# SPDX-License-Identifier: Apache-2.0
"""K22: Houdini file/context resources, linear output, data, viewer and worker isolation."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import time

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from support.paths import artifacts_root


destination = artifacts_root() / "stage3/color/houdini-config"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "report.json"
report = dict(result="running", scope=__doc__, checks=[])
original_env = {name: hou.getenv(name) for name in ("OCIO", "CH_K22_LUT")}
original = OCIO.GetCurrentConfig()
rgb = [0.3, 0.2, 0.1]
size = 32


def record(name, **evidence):
    report["checks"].append(dict(name=name, **evidence))
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def activate(path, context):
    hou.putenv("CH_K22_LUT", context)
    hou.putenv("OCIO", str(path))
    hou.Color.reloadOCIO()
    resolved = Path(hou.Color.ocio_configPath()).resolve()
    assert resolved == path.resolve() and resolved.is_file(), (resolved, path)
    # Create an independent file-backed processor, with the same context the
    # host resolved. No serialized export may mask a broken relative search.
    config = OCIO.Config.CreateFromFile(str(resolved))
    return config


def profile(label, working, gain):
    directory = destination / label
    (directory / "luts" / label).mkdir(parents=True, exist_ok=True)
    lut = directory / "luts" / label / "input.spi1d"
    lut.write_text(
        f"Version 1\nFrom 0.0 1.0\nLength 2\nComponents 3\n{{\n0 0 0\n{gain} {gain} {gain}\n}}\n",
        encoding="utf-8",
    )
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    # Keep the bundled config's own searches and add an intentionally relative,
    # context-dependent resource unique to this file's directory.
    config.addSearchPath("luts/${CH_K22_LUT}")
    config.addEnvironmentVar("CH_K22_LUT", "missing-default")
    config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
    source = OCIO.ColorSpace(name="K22 Input")
    source.setTransform(OCIO.FileTransform(src="input.spi1d"), OCIO.COLORSPACE_DIR_TO_REFERENCE)
    config.addColorSpace(source)
    path = directory / "config.ocio"
    path.write_text(config.serialize(), encoding="utf-8")
    reference = activate(path, label)
    expected = reference.getProcessor("K22 Input", OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB(rgb)
    assert np.isfinite(expected).all() and not np.allclose(expected, rgb), expected
    record("Resolved config and context-dependent relative LUT", config=str(path), context=label, expected=expected)
    return path, reference


geo = hou.node("/obj").createNode("geo", "k22_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
outward = geo.createNode("reverse")
outward.setInput(0, grid)
uv = geo.createNode("attribwrangle")
uv.setInput(0, outward)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "k22_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
texture = destination / "encoded.exr"
writer = OIIO.ImageOutput.create(str(texture))
assert writer.open(str(texture), OIIO.ImageSpec(8, 8, 3, OIIO.FLOAT))
assert writer.write_image(np.broadcast_to(np.array(rgb, np.float32), (8, 8, 3)).copy())
assert writer.close()
material = dict(
    key="surface",
    kind="graph",
    graph=dict(
        schema_version=1,
        nodes=[
            dict(
                id="file",
                definition="cycles/image_texture/1",
                parameters=dict(
                    filename=str(texture), colorspace="K22 Input", interpolation="closest", alpha_type="channel_packed"
                ),
            ),
            dict(id="light", definition="cycles/emission/1", parameters=dict(strength=1.0)),
        ],
        links=[{"from": dict(node="file", socket="color"), "to": dict(node="light", socket="color")}],
        surface=dict(node="light", socket="emission"),
    ),
)
scene = dict(
    geometry=[ch.geometry_request(geo, uv, material="surface")],
    materials=[material],
    camera_path=camera.path(),
    background=(0, 0, 0),
)


def pixels(frame, name):
    plane = frame["planes"][name]
    return np.frombuffer(plane["pixels"], np.float32).reshape(size, size, plane["channels"])


def reference_rgb(config):
    return config.getProcessor("K22 Input", OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB(rgb)


def check_frame(frame, config, tolerance=1e-4, data=True):
    actual = pixels(frame, "combined")[16, 16, :3]
    expected = reference_rgb(config)
    assert np.allclose(actual, expected, rtol=0, atol=tolerance), (actual, expected)
    assert frame["working_space"] == config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
    if data:
        # The independent scene has a +Z surface exactly five units from camera.
        assert np.allclose(pixels(frame, "normal")[16, 16], [0, 0, 1], atol=1e-5)
        assert abs(float(pixels(frame, "depth")[16, 16, 0]) - 5) < 0.01
    return actual.tolist()


def check_output(session, frame, config, label):
    path = destination / (label + ".exr")
    session.write_exr(path, dict(half_color=False, compression="zip"))
    reader = OIIO.ImageInput.open(str(path))
    assert reader, OIIO.geterror()
    spec, actual = reader.spec(), np.asarray(reader.read_image(format=OIIO.FLOAT))
    reader.close()
    assert spec.get_string_attribute("oiio:ColorSpace") == frame["working_space"]
    assert np.allclose(
        actual[15, 16, [spec.channelnames.index(c) for c in ("R", "G", "B")]], reference_rgb(config), rtol=0, atol=1e-4
    )
    for name in ("normal", "depth", "position"):
        channels = session.frame_info()["pass_metadata"][name]["channels"]
        indices = [spec.channelnames.index(name + "." + channel) for channel in channels]
        assert np.array_equal(
            actual[:, :, indices].copy().view(np.uint32), np.flipud(pixels(frame, name)).copy().view(np.uint32)
        ), name
    record("Working-linear EXR and bit-exact numeric AOVs", file=str(path), working_space=frame["working_space"])


def check_viewer(session, frame, source, target, data=True):
    display = target.getDefaultDisplay()
    view = target.getDefaultView(display)
    working = source.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
    reference = (
        OCIO.Config.GetProcessorFromConfigs(source, working, target, display, view, OCIO.TRANSFORM_DIR_FORWARD)
        .getDefaultCPUProcessor()
        .applyRGBA(pixels(frame, "combined")[16, 16].tolist())
    )
    image = session.display_image(display=display, view=view)
    actual = np.frombuffer(image["rgba"], np.uint8).reshape(size, size, 4)[15, 16]
    expected = np.floor(np.clip(reference, 0, 1) * 255 + 0.5).astype(np.uint8)
    assert np.max(np.abs(actual.astype(np.int16) - expected.astype(np.int16))) <= 1, (actual, expected)
    if data:
        plain = session.display_image(**{"pass": "normal"})["rgba"]
        adjusted = session.display_image(**{"pass": "normal", "exposure": 8, "gamma": 2.2})["rgba"]
        assert plain == adjusted
    record(
        "Independent viewer transform and raw data preview",
        display=display,
        view=view,
        source=working,
        actual=actual.tolist(),
        expected=expected.tolist(),
    )


def first_frame(session):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = session.status()
        assert status["state"] != "failed", status
        frame = session.frame()
        if frame and frame["generation"] == status["generation"]:
            return frame
        time.sleep(0.01)
    raise TimeoutError(session.status())


try:
    bundled = Path(hou.Color.ocio_configPath())
    assert bundled.is_file(), bundled
    record("Houdini bundled/default resolved file is readable", config=str(bundled))
    a_path, a = profile("session-a", "ACEScg", 0.5)
    b_path, b = profile("session-b", "Linear Rec.709 (sRGB)", 1.4)
    for device in ("CPU", ch.default_device()):
        for execution, path, config, label in (
            (execution, *profile)
            for execution in ("sync", "worker")
            for profile in ((a_path, a, "session-a"), (b_path, b, "session-b"))
        ):
            activate(path, label)
            ch.synchronize_color_configuration()
            assert Path(ch.color_policy()["config_path"]).resolve() == path.resolve()
            with ch.Session(
                device=device, width=size, height=size, samples=8, execution=execution, use_texture_cache=False
            ) as session:
                frame = session.wait(session.apply(scene), timeout=60)
                record(
                    "Session resource diagnostics",
                    execution=execution,
                    device=device,
                    config=str(path),
                    texture=str(texture),
                    texture_exists=texture.is_file(),
                    source_diagnostics=session.status()["source_diagnostics"],
                )
                assert not session.status()["source_diagnostics"], session.status()["source_diagnostics"]
                measured = check_frame(frame, config)
                if execution == "worker":
                    assert session.status()["renderer_pid"] != os.getpid()
                check_output(
                    session, frame, config, execution + "-" + label + ("-cpu" if device == "CPU" else "-optix")
                )
                check_viewer(session, frame, config, config)
                record(
                    "Session loads Houdini source config",
                    execution=execution,
                    device=device,
                    config=str(path),
                    rgb=measured,
                )
        activate(a_path, "session-a")
        ch.synchronize_color_configuration()
        with ch.Session(device=device, width=size, height=size, samples=8, execution="sync") as first:
            accepted = first.wait(first.apply(scene))
            # Compatible in-process owners are legal and leave both images usable.
            with ch.Session(device=device, width=size, height=size, samples=8, execution="sync") as compatible:
                check_frame(compatible.wait(compatible.apply(scene)), a)
                check_frame(first.frame(), a)
            activate(b_path, "session-b")
            ch.synchronize_color_configuration()
            try:
                with ch.Session(device=device, width=size, height=size, samples=8, execution="sync") as conflict:
                    conflict.wait(conflict.apply(scene))
            except RuntimeError as error:
                assert "worker" in str(error) and "config" in str(error).lower(), str(error)
                diagnostic = str(error)
            else:
                raise AssertionError("Conflicting overlapping sync color configurations were accepted")
            assert first.frame()["planes"] == accepted["planes"]
            check_frame(first.frame(), a)
        with ch.Session(device=device, width=size, height=size, samples=8, execution="sync") as sequential:
            check_frame(sequential.wait(sequential.apply(scene)), b)
        record(
            "Sync same-config overlap, conflicting-config diagnostic and sequential recovery",
            device=device,
            diagnostic=diagnostic,
        )
        # Hold both engines live: changing host config must not cancel/join the
        # first engine, overwrite its context, or reinterpret its pixels.
        with ExitStack() as owned:
            activate(a_path, "session-a")
            ch.synchronize_color_configuration()
            first = owned.enter_context(
                ch.Session(
                    device=device,
                    width=size,
                    height=size,
                    samples=16777216,
                    execution="worker",
                    use_texture_cache=False,
                )
            )
            first.apply(scene)
            first_frame(first)
            first_before = first.status()
            activate(b_path, "session-b")
            ch.synchronize_color_configuration()
            second = owned.enter_context(
                ch.Session(
                    device=device,
                    width=size,
                    height=size,
                    samples=16777216,
                    execution="worker",
                    use_texture_cache=False,
                )
            )
            second.apply(scene)
            second_frame = first_frame(second)
            first_after = first.status()
            assert first_after["state"] == "rendering", first_after
            assert first_after["generation"] == first_before["generation"]
            assert not first_after["color_update_pending"]
            assert first_after["renderer_pid"] != second.status()["renderer_pid"]
            first_image = first.frame()
            # Extended float32 accumulation may drift a few 1e-4 in a live
            # emission render; finite 8-sample output above keeps a 1e-4 bound.
            check_frame(first_image, a, tolerance=1e-3, data=False)
            check_frame(second_frame, b, tolerance=1e-3, data=False)
            check_viewer(first, first_image, a, b, data=False)
            first.pause()
            deadline = time.monotonic() + 10
            while first.status()["state"] != "paused" and time.monotonic() < deadline:
                time.sleep(0.01)
            assert first.status()["state"] == "paused"
            second.cancel()
            before = second.status()["generation"]
            # Explicit update moves only this owner to current Houdini config.
            first.apply({})
            assert first.status()["state"] == "paused", first.status()
            first.pause(False)
            check_frame(first_frame(first), b, tolerance=1e-3, data=False)
            assert second.status()["state"] == "cancelled" and second.status()["generation"] == before
            record(
                "Two worker configs render concurrently; explicit update preserves pause/cancel ownership",
                device=device,
                first_pid=first_after["renderer_pid"],
                second_pid=second.status()["renderer_pid"],
            )
    report["result"] = "passed"
except Exception as error:
    report.update(result="failed", error=str(error))
    raise
finally:
    for name, value in original_env.items():
        hou.unsetenv(name) if value is None else hou.putenv(name, value)
    hou.Color.reloadOCIO()
    OCIO.SetCurrentConfig(original)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
