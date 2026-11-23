# SPDX-License-Identifier: Apache-2.0
"""Native catcher authoring and independently checked direct-light compositing."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import sys
import time
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import materials, native_scene, object_properties, light_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
destination = artifacts_root() / "stage3/passes/shadow-catcher"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-shadow-catcher-smoke.json"
report = dict(result="running", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=[])


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def exr(path, frame, independent_render=False):
    image = OIIO.ImageInput.open(str(path))
    assert image
    spec = image.spec()
    pixels = image.read_image(format=OIIO.FLOAT)
    for name in ("combined", "shadow_catcher"):
        channels = "RGBA" if name == "combined" else "RGB"
        indices = [spec.channelnames.index(c if name == "combined" else name + "." + c) for c in channels]
        actual = pixels[:, :, indices]
        expected = np.flipud(plane(frame, name))
        if independent_render:
            # A new GPU session can accumulate float32 samples in a different
            # order. File round-trips remain bit-exact; separate renders allow
            # only small floating-point differences, not image/noise changes.
            assert np.allclose(actual, expected, rtol=1e-5, atol=3e-6), name
            report.setdefault("batch_max_abs", {})[name] = float(np.max(np.abs(actual - expected)))
        else:
            assert np.array_equal(actual, expected), name
    assert spec.get_string_attribute("compression") in ("zip", "zips")
    image.close()


try:
    scene = hou.node("/obj").createNode("subnet", "catcher_fixture")
    ground = scene.createNode("geo", "catcher_ground")
    for child in ground.children():
        child.destroy()
    box = ground.createNode("box", "native_ground")
    box.parmTuple("size").set((6, 0.06, 6))
    box.parmTuple("t").set((0, -0.03, 0))
    box.setRenderFlag(True)
    object_properties.install(ground)
    floor_mat = materials.create_material(hou.node("/mat"), "catcher_ground", "diffuse_bsdf")
    floor_mat.node("surface_shader").parmTuple("color").set((0.45, 0.6, 0.7))
    pass_fixture.parm(floor_mat.node("surface_shader"), "roughness").set(0)
    pass_fixture.parm(ground, "shop_materialpath").set(floor_mat.path())
    actor = scene.createNode("geo", "virtual_actor")
    for child in actor.children():
        child.destroy()
    box = actor.createNode("box", "native_actor")
    box.parmTuple("size").set((1.1, 1.2, 1.1))
    box.setRenderFlag(True)
    pass_fixture.set_parameters(actor, dict(tx=-0.6, ty=0.65))
    object_properties.install(actor)
    actor_mat = materials.create_material(hou.node("/mat"), "catcher_actor", "diffuse_bsdf")
    actor_mat.node("surface_shader").parmTuple("color").set((0.2, 0.5, 0.8))
    pass_fixture.parm(actor_mat.node("surface_shader"), "roughness").set(0)
    pass_fixture.parm(actor, "shop_materialpath").set(actor_mat.path())
    sun = scene.createNode("hlight", "sun")
    pass_fixture.set_parameters(sun, dict(light_type="distant", rx=-55, ry=-25, light_intensity=3))
    light_properties.install(sun)
    camera = hou.node("/obj").createNode("cam", "catcher_camera")
    pass_fixture.set_parameters(camera, dict(ty=5, tz=6, rx=-39.805571, projection=1, orthowidth=6.8))
    rop = hou.node("/out").createNode("cycles_ipr", "catcher_output")
    set_rop_devices(rop, device)
    pass_fixture.set_parameters(
        rop,
        dict(
            source_mode="objects",
            objectroot=scene.path(),
            camera=camera.path(),
            resx=96,
            resy=96,
            samples=256,
            # Compare the same sample budget and denoising policy in both the
            # retained session and the independent native ROP render.
            viewer_interactive=0,
            use_denoise=0,
            # Auto deliberately chooses different noise sequences in a viewer
            # and a disk render. This comparison requires identical samples.
            sampling_pattern="blue_noise",
            pass_shadow_catcher=1,
            transparent_background=1,
            max_bounce=0,
            diffuse_bounce=0,
            glossy_bounce=0,
            transmission_bounce=0,
            volume_bounce=0,
        ),
    )
    rop.parmTuple("background").set((0, 0, 0))
    with ch.Session(device=device, width=96, height=96, samples=256, collect_diagnostics=True) as session:

        def render():
            return session.wait(session.apply(native_scene.capture(rop)))

        # No catcher: its multiplier is the identity and Beauty stays ordinary.
        actor.setDisplayFlag(False)
        plate_frame = render()
        plate = plane(plate_frame, "combined").copy()
        assert np.allclose(plane(plate_frame, "shadow_catcher"), 1)
        actor.setDisplayFlag(True)
        regular_frame = render()
        regular = plane(regular_frame, "combined").copy()
        converted = session.status()["geometry_conversions"]
        assert np.allclose(plane(regular_frame, "shadow_catcher"), 1)
        pass_fixture.parm(ground, "cycles_shadow_catcher").set(1)
        caught = render()
        factor = plane(caught, "shadow_catcher")
        foreground = plane(caught, "combined")
        assert (
            np.isfinite(factor).all()
            and np.any(factor < 0.2)
            and np.count_nonzero(np.all(np.isclose(factor, 1, atol=0.001), axis=2)) > 100
        )
        assert np.max(foreground[:, :, 3]) > 0.999 and np.count_nonzero(foreground[:, :, 3] < 0.001) > 100
        composite = foreground[:, :, :3] + (1 - foreground[:, :, 3:4]) * plate[:, :, :3] * factor
        interior = (plate[:, :, 3] > 0.999) & (foreground[:, :, 3] < 0.001)
        rmse = float(np.sqrt(np.mean((composite[interior] - regular[interior, :3]) ** 2)))
        assert rmse < 0.01, rmse
        assert session.status()["geometry_conversions"] == converted
        session.write_exr(destination / "separate.exr")
        exr(destination / "separate.exr", caught)
        report["checks"].append(
            dict(
                name="Identity output without catchers; native OBJ catcher and physical direct shadow; foreground-over-multiplied-plate matches ordinary rendering",
                composite_rmse=rmse,
            )
        )

        # Lights need separate reference visibility. With no reference lighting
        # Cycles intentionally uses identity division, rather than a black plate.
        pass_fixture.parm(sun, "cycles_light_shadow_catcher").set(0)
        virtual_light = render()
        assert np.allclose(plane(virtual_light, "shadow_catcher"), 1)
        pass_fixture.parm(sun, "cycles_light_shadow_catcher").set(1)
        real_light = render()
        assert np.allclose(plane(real_light, "shadow_catcher"), factor, atol=1e-6)
        pass_fixture.parm(sun, "light_intensity").set(0)
        rop.parmTuple("background").set((0.2, 0.2, 0.2))
        real_world = render()
        world_factor = plane(real_world, "shadow_catcher")
        assert np.isfinite(world_factor).all() and world_factor.min() < 0.8
        pass_fixture.parm(rop, "background_shadow_catcher").set(0)
        virtual_world = render()
        virtual_factor = plane(virtual_world, "shadow_catcher")
        report["world_reference_diagnostics"] = dict(
            real_min=float(world_factor.min()),
            virtual_min=float(virtual_factor.min()),
            virtual_max=float(virtual_factor.max()),
            snapshot_reference=native_scene.capture(rop).get("background_shadow_catcher"),
            status=session.status(),
        )
        session.write_exr(destination / "virtual-world.exr")
        assert np.allclose(virtual_factor, 1)
        pass_fixture.parm(rop, "background_shadow_catcher").set(1)
        world_restored = render()
        assert np.allclose(plane(world_restored, "shadow_catcher"), world_factor, atol=1e-6)
        pass_fixture.parm(sun, "light_intensity").set(3)
        rop.parmTuple("background").set((0, 0, 0))
        render()
        assert session.status()["geometry_conversions"] == converted
        report["checks"].append(
            "Real/reference visibility for native analytic lights and World; live virtual/real switches retain geometry"
        )

        # With the explicit pass disabled, Beauty approximates the shadow in
        # its alpha, requiring no separate factor in an artist's quick preview.
        pass_fixture.parm(rop, "pass_shadow_catcher").set(0)
        approximate = render()
        alpha = plane(approximate, "combined")[:, :, 3]
        assert np.count_nonzero(alpha[interior] > 0.2) > 50 and np.count_nonzero(alpha[interior] < 0.001) > 100
        assert "shadow_catcher" not in approximate["planes"]
        pass_fixture.parm(rop, "pass_shadow_catcher").set(1)
        restored = render()
        assert np.allclose(plane(restored, "shadow_catcher"), factor, atol=1e-6)
        assert session.status()["geometry_conversions"] == converted
        pass_fixture.parm(rop, "use_denoise").set(1)
        pass_fixture.parm(rop, "denoiser").set("oidn")
        pass_fixture.parm(rop, "denoise_use_gpu").set(0)
        denoised = render()
        assert np.isfinite(plane(denoised, "shadow_catcher")).all()
        assert plane(denoised, "shadow_catcher").min() < 0.4 and plane(denoised, "combined")[:, :, 3].max() > 0.9
        session.write_exr(destination / "denoised.exr")
        exr(destination / "denoised.exr", denoised)
        assert session.status()["geometry_conversions"] == converted
        report["checks"].append(
            "Approximate Beauty alpha and separate compositing workflows; live switches retain geometry; OIDN and lossless RGB/RGBA EXRs"
        )

        pass_fixture.parm(rop, "use_denoise").set(0)
        pass_fixture.parm(rop, "samples").set(1048576)
        target = session.apply(native_scene.capture(rop))
        session.set_preview_pass("shadow_catcher")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = session.frame_info()
            if info and info["generation"] == target and not info["complete"] and "shadow_catcher" in info["passes"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("No progressive shadow-catcher preview")
        metadata = info["pass_metadata"]["shadow_catcher"]
        assert metadata["channels"] == ["R", "G", "B"] and not metadata["color"]
        preview = session.display_image(**{"pass": "shadow_catcher"})
        assert len(preview["rgba"]) == 96 * 96 * 4
        session.open_native_display("null:")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = session.frame_info()
            if set(info["passes"]) == set(info["enabled_passes"]):
                session.present_native()
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Native shadow-catcher planes stalled")
        session.close_native_display()
        session.cancel()
        pass_fixture.parm(rop, "samples").set(256)
        final = render()
        assert session.status()["geometry_conversions"] == converted
        pass_fixture.parm(ground, "cycles_shadow_catcher").set(0)
        ordinary = render()
        assert np.allclose(plane(ordinary, "shadow_catcher"), 1) and np.allclose(
            plane(ordinary, "combined"), regular, atol=1e-6
        )
        pass_fixture.parm(ground, "cycles_shadow_catcher").set(1)
        final = render()
        report["checks"].append(
            "Progressive raw RGB preview/native planes; immediate Stop/Render; removing the last catcher restores ordinary Beauty without geometry recopy"
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    hip = destination / "shadow-catcher-authoring.hiplc"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    rop = hou.node("/out/catcher_output")
    pass_fixture.set_parameters(rop, dict(trange=0, picture=str(destination / "native-batch.exr")))
    rop.render(frame_range=(1, 1), ignore_inputs=True)
    exr(destination / "native-batch.exr", final, independent_render=True)
    report["checks"].append("Native ROP/SOHO batch and optional OBJ catcher settings survive HIP reload")
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(report_path)
