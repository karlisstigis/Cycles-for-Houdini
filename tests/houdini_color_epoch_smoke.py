# SPDX-License-Identifier: Apache-2.0
"""Independent OCIO references and independently retained worker render sessions."""

from support.paths import artifacts_root
from array import array
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

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/epochs"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_env = hou.getenv("OCIO")
serial = 0


def profile(working, view=None, scale=None, working_lut=None, interchange=True, inactive=False):
    global serial
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
    if view:
        config.setActiveViews(view)
    if scale is not None:
        space = OCIO.ColorSpace(name="Studio Input")
        space.setTransform(
            OCIO.MatrixTransform(matrix=[scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, 1]),
            OCIO.COLORSPACE_DIR_TO_REFERENCE,
        )
        config.addColorSpace(space)
        if inactive:
            config.setInactiveColorSpaces("Studio Input")
    if working_lut is not None:
        space = OCIO.ColorSpace(name="Studio Working")
        space.setTransform(
            OCIO.FileTransform(src=str(working_lut), interpolation=OCIO.INTERP_LINEAR), OCIO.COLORSPACE_DIR_TO_REFERENCE
        )
        config.addColorSpace(space)
    if not interchange:
        config.setRole("aces_interchange", "")
    serial += 1
    path = destination / f"profile-{serial}.ocio"
    path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(path))
    hou.Color.reloadOCIO()
    return OCIO.GetCurrentConfig()


geo = hou.node("/obj").createNode("geo", "color_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
# Positive height follows the explicitly +Z-facing receiver, rather than the
# native XY Grid's -Z normal. The luminance reference below remains unchanged.
outward = geo.createNode("reverse", "positive_z_receiver")
outward.setInput(0, grid)
assert np.allclose(tuple(outward.geometry().prims()[0].normal()), (0, 0, 1))
uv = geo.createNode("attribwrangle")
uv.setInput(0, outward)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "color_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
texture = destination / "encoded.exr"
output = OIIO.ImageOutput.create(str(texture))
spec = OIIO.ImageSpec(256, 256, 3, OIIO.FLOAT)
assert (
    output.open(str(texture), spec)
    and output.write_image(np.broadcast_to(np.array([0.8, 0.4, 0.1], np.float32), (256, 256, 3)).copy())
    and output.close()
)


def node(id, type, **parameters):
    return dict(id=id, definition=f"cycles/{type}/1", parameters=parameters)


def end(id, socket):
    return dict(node=id, socket=socket)


def link(a, output, b, input):
    return {"from": end(a, output), "to": end(b, input)}


def graph(space="sRGB Encoded Rec.709 (sRGB)", displacement=False):
    nodes = [
        node(
            "file",
            "image_texture",
            filename=str(texture),
            colorspace=space,
            interpolation="closest",
            alpha_type="channel_packed",
        ),
        node("light", "emission", strength=1.0),
    ]
    links = [link("file", "color", "light", "color")]
    result = dict(schema_version=1, nodes=nodes, links=links, surface=end("light", "emission"))
    if displacement:
        nodes.extend(
            [node("gray", "rgb_to_bw"), node("offset", "displacement", midlevel=0.0, scale=1.0, space="object")]
        )
        links.extend([link("file", "color", "gray", "color"), link("gray", "val", "offset", "height")])
        result.update(displacement=end("offset", "displacement"), displacement_method="true")
    return dict(key="surface", kind="graph", graph=result)


request = ch.geometry_request(geo, uv, material="surface")


def initial(material=None):
    return dict(geometry=[request], materials=[material or graph()], camera_path=camera.path(), background=(0, 0, 0))


def plane(frame, name):
    data = array("f")
    data.frombytes(frame["planes"][name]["pixels"])
    return np.asarray(data).reshape((32, 32, frame["planes"][name]["channels"]))


def expected(config, space="sRGB Encoded Rec.709 (sRGB)"):
    return config.getProcessor(space, OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB([0.8, 0.4, 0.1])


def check(frame, config, space="sRGB Encoded Rec.709 (sRGB)", tolerance=1e-4):
    target = expected(config, space)
    measured = plane(frame, "combined")[16, 16, :3]
    assert np.allclose(measured, target, rtol=0, atol=tolerance), (measured, target)
    assert frame["working_space"] == config.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName()
    return measured.tolist()


def first_frame(session):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        frame = session.frame()
        if frame and frame["generation"] == session.status()["generation"]:
            return frame
        assert session.status()["state"] != "failed", session.status()
        time.sleep(0.01)
    raise AssertionError("No progressive image")


def reject_retaining_frames(session, update, peers, expected_error):
    before = session.status()
    frame = session.frame()
    peer_states = [(peer.status(), peer.frame()) for peer in peers]
    try:
        generation = session.apply(update)
        session.wait(generation, timeout=30)
    except RuntimeError as error:
        assert expected_error in str(error), str(error)
    else:
        raise AssertionError("Invalid candidate was accepted during an OCIO epoch transition")
    after = session.status()
    retained = session.frame()
    # A rejected explicit update may leave this owner awaiting its new color
    # policy. Its committed scene and peers must retain the accepted resources.
    for key in ("committed_generation", "backend_state"):
        assert after[key] == before[key], (key, before, after)
    for key in ("sequence", "generation", "color_epoch", "render_color_policy", "working_space", "planes"):
        assert retained[key] == frame[key], key
    for peer, (old_status, old_frame) in zip(peers, peer_states):
        current = peer.status()
        for key in ("generation", "state", "color_update_pending"):
            assert current[key] == old_status[key], (key, old_status, current)
        # Paused/cancelled peers cannot publish while the candidate is rejected.
        current_frame = peer.frame()
        for key in ("generation", "color_epoch", "render_color_policy", "working_space", "planes"):
            assert current_frame[key] == old_frame[key], (key, peer.status())


def output_space(session, name):
    path = destination / name
    session.write_exr(path)
    reader = OIIO.ImageInput.open(str(path))
    assert reader
    result = reader.spec().get_string_attribute("oiio:ColorSpace")
    reader.close()
    return result


def write_lut(path, scale):
    path.write_text(
        f"Version 1\nFrom 0.0 4.0\nLength 2\nComponents 3\n{{\n0 0 0\n{scale * 4} {scale * 4} {scale * 4}\n}}\n",
        encoding="utf-8",
    )


def check_display(session, processor, rgba):
    policy = ch.color_policy()
    display = policy["default_display"]
    view = policy["default_views"][display]
    reference = processor.applyRGBA(rgba)
    actual = session.display_image(display=display, view=view)["rgba"]
    at = ((31 - 16) * 32 + 16) * 4
    assert all(abs(actual[at + c] - round(max(0, min(1, reference[c])) * 255)) <= 1 for c in range(4)), (
        list(actual[at : at + 4]),
        reference,
    )
    return list(actual[at : at + 4])


reports = []
try:
    for device in ("CPU", ch.default_device()):
        checks = []
        source = profile("ACEScg")
        policy = ch.color_policy()
        with ExitStack() as owned:

            def create(samples=32, cache=False):
                return owned.enter_context(
                    ch.Session(
                        device=device,
                        width=32,
                        height=32,
                        samples=samples,
                        collect_diagnostics=True,
                        use_texture_cache=cache,
                        auto_texture_cache=cache,
                        execution="worker",
                    )
                )

            finished = create()
            frame = finished.wait(finished.apply(initial()))
            check(frame, source)
            generation = frame["generation"]
            geometry = finished.status()["geometry_conversions"]
            builds = finished.diagnostics()["material_builds"]
            # A different default view changes the full config ID, but not the
            # source-space/input processors or active texture resources.
            display = policy["default_display"]
            views = policy["views"][display]
            alternate = next(view for view in views if view != policy["default_views"][display])
            target = profile("ACEScg", view=alternate)
            updated = ch.color_policy()
            assert updated["configuration_id"] != policy["configuration_id"]
            assert updated["render_policy_id"] == policy["render_policy_id"]
            ch.synchronize_color_configuration()
            assert not finished.textures_changed() and finished.status()["generation"] == generation
            assert finished.diagnostics()["material_builds"] == builds
            checks.append(dict(name="Display-only configuration edit retains generation, shader and image identity"))
            paused = create()
            paused.wait(paused.apply(initial()))
            paused.pause()
            cancelled = create()
            cancelled.wait(cancelled.apply(initial()))
            cancelled.cancel()
            live = create(16777216)
            live.apply(initial())
            first_frame(live)
            old_generations = [s.status()["generation"] for s in (finished, live, paused, cancelled)]
            target = profile("Linear Rec.709 (sRGB)")
            # Each worker owns its old config until that owner gets an update.
            # Observing Houdini's new config must never join the other engines.
            time.sleep(0.05)
            assert live.frame_info()["working_space"] == "ACEScg"
            assert live.status()["state"] == "rendering", live.status()
            ch.synchronize_color_configuration()
            states = [s.status() for s in (finished, live, paused, cancelled)]
            assert not any(s["color_update_pending"] for s in states)
            assert [s["generation"] for s in states] == old_generations
            assert [s["state"] for s in states] == ["complete", "rendering", "paused", "cancelled"], states
            assert not live.frame_info()["complete"]
            check(live.frame(), source, tolerance=1e-3)
            try:
                live.write_exr(destination / "incomplete-progressive.exr")
            except RuntimeError as error:
                assert "complete" in str(error).lower()
            else:
                raise AssertionError("Incomplete progressive output was accepted as a finished render")
            checks.append(
                dict(name="A host config change preserves live rendering; incomplete output cannot report completion")
            )
            assert output_space(finished, "old-frame-" + ("cpu" if device == "CPU" else "optix") + ".exr") == "ACEScg"
            current = ch.color_policy()
            display = current["default_display"]
            view = current["default_views"][display]
            rgba = list(plane(frame, "combined")[16, 16])
            reference = (
                OCIO.Config.GetProcessorFromConfigs(source, "ACEScg", target, display, view, OCIO.TRANSFORM_DIR_FORWARD)
                .getDefaultCPUProcessor()
                .applyRGBA(rgba)
            )
            actual = finished.display_image(display=display, view=view)["rgba"]
            at = ((31 - 16) * 32 + 16) * 4
            assert all(abs(actual[at + c] - round(max(0, min(1, reference[c])) * 255)) <= 1 for c in range(4))
            checks.append(
                dict(
                    name="Independent workers retain pause/cancel and old output/display source meaning",
                    states=[s["state"] for s in states],
                )
            )
            live.pause()
            deadline = time.monotonic() + 10
            while live.status()["state"] != "paused" and time.monotonic() < deadline:
                time.sleep(0.01)
            assert live.status()["state"] == "paused"
            # A parsed camera with an unsupported iris mode reaches the core
            # candidate guard, before graph preparation or engine mutation.
            reject_retaining_frames(
                finished,
                dict(camera_path=camera.path(), camera_dof=dict(image_iris_method=3)),
                [paused, cancelled],
                "Image Iris requires",
            )
            # This fails full camera validation after pending-color shader
            # preparation, exercising rejection beyond the early iris guard.
            reject_retaining_frames(finished, dict(camera=dict(near=-1)), [paused, cancelled], "Invalid clipping range")
            # Progressive publication may deliver one final image after pause.
            # Its policy and committed scene must still remain independent.
            assert live.status()["state"] == "paused" and live.status()["generation"] == old_generations[1]
            check(live.frame(), source, tolerance=1e-3)
            checks.append(
                dict(
                    name="Early Image Iris and prepared camera rejection retains independent owners and old frame bytes"
                )
            )
            frame = finished.wait(
                finished.apply(dict(initial(), camera=dict(near=0.001), camera_dof=dict(image_iris_method=0)))
            )
            check(frame, target)
            assert (
                finished.status()["geometry_conversions"] == geometry
                and finished.diagnostics()["material_builds"] == builds + 1
            )
            paused_generation = paused.apply({})
            assert paused.status()["state"] == "paused" and not paused.status()["color_update_pending"]
            paused.pause(False)
            check(paused.wait(paused_generation), target)
            live.apply({})
            live.pause(False)
            check(first_frame(live), target, tolerance=1e-3)
            assert cancelled.status()["state"] == "cancelled" and cancelled.status()["generation"] == old_generations[3]
            checks.append(
                dict(
                    name="Each owner updates retained resources before restart; unupdated canceled render stays stopped"
                )
            )
            # A same-named input transform must invalidate caches, too. Run both
            # uncached and generated-tx paths against public OCIO references.
            for cache in (False, True):
                a = profile("ACEScg", scale=0.7)
                ch.synchronize_color_configuration()
                session = create(cache=cache)
                check(session.wait(session.apply(initial(graph("Studio Input")))), a, "Studio Input")
                old_geometry = session.status()["geometry_conversions"]
                old_policy = session.frame_info()["render_color_policy"]
                b = profile("ACEScg", scale=1.4)
                ch.synchronize_color_configuration()
                changed = session.wait(session.apply({}))
                check(changed, b, "Studio Input")
                assert (
                    changed["render_color_policy"] != old_policy
                    and session.status()["geometry_conversions"] == old_geometry
                )
                checks.append(dict(name="Same-named input transform updates image conversion", cache=cache))
                session.close()
            # Inactive spaces are hidden from menus but remain legal in saved
            # graphs. Their transforms must participate in engine identity;
            # changing menu visibility alone must not invalidate rendering.
            for cache in (False, True):
                a = profile("ACEScg", scale=0.7, inactive=True)
                ch.synchronize_color_configuration()
                assert "Studio Input" in a.getInactiveColorSpaces()
                session = create(cache=cache)
                check(session.wait(session.apply(initial(graph("Studio Input")))), a, "Studio Input")
                old_geometry = session.status()["geometry_conversions"]
                old_frame = session.frame_info()
                b = profile("ACEScg", scale=1.4, inactive=True)
                ch.synchronize_color_configuration()
                changed = session.wait(session.apply({}))
                check(changed, b, "Studio Input")
                assert changed["render_color_policy"] != old_frame["render_color_policy"]
                assert session.status()["geometry_conversions"] == old_geometry
                before = ch.color_policy()
                generation = session.status()["generation"]
                visible = profile("ACEScg", scale=1.4)
                after = ch.color_policy()
                assert before["configuration_id"] != after["configuration_id"]
                assert before["render_policy_id"] == after["render_policy_id"]
                ch.synchronize_color_configuration()
                assert not session.textures_changed()
                assert session.status()["generation"] == generation
                assert "Studio Input" not in visible.getInactiveColorSpaces()
                checks.append(
                    dict(name="Inactive named inputs update; visibility-only changes retain the render", cache=cache)
                )
                session.close()
            # Height conversion and luminance use the new shader matrices.
            for working in ("ACEScg", "Linear Rec.709 (sRGB)", "ACEScg"):
                config = profile(working)
                ch.synchronize_color_configuration()
                frame = finished.wait(finished.apply(dict(materials=[graph(displacement=True)])))
                rgb = expected(config)
                ap0 = (
                    config.getProcessor(OCIO.ROLE_SCENE_LINEAR, "aces_interchange")
                    .getDefaultCPUProcessor()
                    .applyRGB(rgb)
                )
                xyz = (
                    config.getProcessor(OCIO.BuiltinTransform(style="UTILITY - ACES-AP0_to_CIE-XYZ-D65_BFD"))
                    .getDefaultCPUProcessor()
                    .applyRGB(ap0)
                )
                height = float(plane(frame, "position")[16, 16, 2])
                assert abs(height - xyz[1]) < 1e-4, (working, height, xyz)
                assert finished.status()["geometry_conversions"] == geometry
                checks.append(
                    dict(
                        name="Textured true displacement rebuilt from original geometry with current luminance",
                        working_space=working,
                        height=height,
                        reference=xyz[1],
                    )
                )
            # Frozen source processors must survive replacement of LUT files,
            # even when both the colorspace name and serialized config agree.
            lut = destination / "working.spi1d"
            write_lut(lut, 0.5)
            old = profile("Studio Working", working_lut=lut)
            ch.synchronize_color_configuration()
            old_frame = finished.wait(finished.apply(dict(materials=[graph("raw")])))
            old_rgba = plane(old_frame, "combined")[16, 16].tolist()
            old_policy = ch.color_policy()
            display = old_policy["default_display"]
            view = old_policy["default_views"][display]
            old_processor = old.getProcessor(
                "Studio Working", display, view, OCIO.TRANSFORM_DIR_FORWARD
            ).getDefaultCPUProcessor()
            old_display = check_display(finished, old_processor, old_rgba)
            assert np.allclose(
                old.getProcessor("Studio Working", "aces_interchange")
                .getDefaultCPUProcessor()
                .applyRGB([0.8, 0.4, 0.1]),
                [0.4, 0.2, 0.05],
                atol=1e-5,
            )
            write_lut(lut, 0.25)
            new = profile("Studio Working", working_lut=lut)
            ch.synchronize_color_configuration()
            assert ch.color_policy()["render_policy_id"] != old_policy["render_policy_id"]
            assert np.allclose(
                new.getProcessor("Studio Working", "aces_interchange")
                .getDefaultCPUProcessor()
                .applyRGB([0.8, 0.4, 0.1]),
                [0.2, 0.1, 0.025],
                atol=1e-5,
            )
            assert check_display(finished, old_processor, old_rgba) == old_display
            assert (
                output_space(finished, "lut-source-" + ("cpu" if device == "CPU" else "optix") + ".exr")
                == "Studio Working"
            )
            new_frame = finished.wait(finished.apply({}))
            new_rgba = plane(new_frame, "combined")[16, 16].tolist()
            new_processor = new.getProcessor(
                "Studio Working", display, view, OCIO.TRANSFORM_DIR_FORWARD
            ).getDefaultCPUProcessor()
            assert check_display(finished, new_processor, new_rgba) != old_display
            checks.append(
                dict(
                    name="Same-name external LUT replacement preserves compiled source meaning and refreshes new rendering",
                    old_display=old_display,
                )
            )
            previous_display = check_display(finished, new_processor, new_rgba)
            write_lut(lut, 0.4)
            ch.synchronize_color_configuration()
            # Build an independent config instance: Houdini's unchanged
            # instance may still have its own already compiled processor cache.
            direct = OCIO.Config.CreateFromStream(new.serialize())
            direct.setWorkingDir(new.getWorkingDir())
            assert np.allclose(
                direct.getProcessor("Studio Working", "aces_interchange")
                .getDefaultCPUProcessor()
                .applyRGB([0.8, 0.4, 0.1]),
                [0.32, 0.16, 0.04],
                atol=1e-5,
            )
            assert check_display(finished, new_processor, new_rgba) == previous_display
            disk_frame = finished.wait(finished.apply({}))
            new_rgba = plane(disk_frame, "combined")[16, 16].tolist()
            new_processor = direct.getProcessor(
                "Studio Working", display, view, OCIO.TRANSFORM_DIR_FORWARD
            ).getDefaultCPUProcessor()
            assert check_display(finished, new_processor, new_rgba) != previous_display
            checks.append(dict(name="Disk-only working LUT edits refresh the updated worker without a config reload"))
            old_generation = finished.status()["generation"]
            lut.unlink()
            try:
                ch.synchronize_color_configuration()
            except RuntimeError as error:
                assert "working.spi1d" in str(error)
            else:
                raise AssertionError("A missing working-space LUT did not report an error")
            assert not finished.status()["color_update_pending"] and finished.status()["generation"] == old_generation
            assert (
                output_space(finished, "missing-lut-" + ("cpu" if device == "CPU" else "optix") + ".exr")
                == "Studio Working"
            )
            write_lut(lut, 0.4)
            ch.synchronize_color_configuration()
            assert check_display(finished, new_processor, new_rgba)
            checks.append(dict(name="Failed LUT reload preserves frame/output and retains file-repair detection"))
            # Missing cross-config roles are a visible diagnostic, while raw
            # data passes and source-tagged EXR remain usable.
            finished.open_native_display("null:")
            finished.present_native()
            normal_before = finished.display_image(**{"pass": "normal"})["rgba"]
            profile("Linear Rec.709 (sRGB)", interchange=False)
            ch.synchronize_color_configuration()
            try:
                finished.display_image()
            except RuntimeError as error:
                assert "aces_interchange" in str(error)
            else:
                raise AssertionError("Missing interchange role silently reinterpreted an old frame")
            # Hython has no Qt GUI surface. Exercise the Render Panel converter
            # on its CPU path with the same owned frame the GUI queue uses.
            from cycles_houdini.viewer.gpu_display import Converter

            converter = Converter.__new__(Converter)
            converter.failure = "Headless display"
            retained = finished.display_frame()[0]
            raw = converter(retained, {})
            assert "raw color" in raw["previous_color_notice"]
            assert bytes(raw["rgba"]) == bytes(finished.display_image(raw=True)["rgba"])
            component = converter(retained, dict(channels="r"))
            assert component["previous_color_notice"] == raw["previous_color_notice"]
            rgb = np.frombuffer(component["rgba"], np.uint8).reshape(-1, 4)[:, :3]
            assert np.all(rgb == rgb[:, :1])
            try:
                finished.present_native()
            except ch._native.PreviousColorConfiguration as notice:
                assert "Previous color configuration" in str(notice)
            else:
                raise AssertionError("Native viewer submitted an incompatible retained frame")
            assert finished.display_image(**{"pass": "normal", "exposure": 8})["rgba"] == normal_before
            assert (
                output_space(finished, "missing-role-" + ("cpu" if device == "CPU" else "optix") + ".exr")
                == "Studio Working"
            )
            profile("ACEScg")
            ch.synchronize_color_configuration()
            assert finished.display_image()
            assert "previous_color_notice" not in converter(retained, {})
            finished.present_native()
            finished.close_native_display()
            checks.append(
                dict(
                    name="Missing interchange role diagnoses old-frame display; data/output survive and role restoration recovers"
                )
            )
            live.cancel()
        assert all(s.status()["device_allocation_bytes"] == 0 for s in (finished, live, paused, cancelled))
        reports.append(dict(device=device, result="passed", checks=checks))
finally:
    hou.putenv("OCIO", original_env or "")
    hou.Color.reloadOCIO()
    OCIO.SetCurrentConfig(original)
report = dict(
    result="passed",
    scope="Independent worker Rec.709/ACEScg color epochs, immutable frames and same-name texture transforms; complete file-rule and Solaris color behavior are separate gates",
    devices=reports,
)
(destination / "houdini-color-epoch-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
