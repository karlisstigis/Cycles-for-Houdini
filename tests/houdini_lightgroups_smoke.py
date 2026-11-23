# SPDX-License-Identifier: Apache-2.0
"""Independent contribution partition, native assignments, updates and EXR output."""

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
from cycles_houdini.passes import custom_configuration, catalogue

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/passes/lightgroups"
destination.mkdir(parents=True, exist_ok=True)
path = destination / "houdini-lightgroups-smoke.json"
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
report = dict(result="running", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=[])


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def groups(rop, names):
    pass_fixture.parm(rop, "lightgroup_count").set(len(names))
    for index, name in enumerate(names, 1):
        pass_fixture.parm(rop, "lightgroup_name" + str(index)).set(name)
    return custom_configuration(rop, hou.time())


def split_matches(frame, names):
    combined = plane(frame, "combined")[:, :, :3]
    grouped = sum(plane(frame, "lightgroup_" + name) for name in names)
    assert np.allclose(combined, grouped, atol=2e-5, rtol=3e-5), float(np.abs(combined - grouped).max())


try:
    scene = hou.node("/obj").createNode("subnet", "lightgroup_fixture")
    receiver = scene.createNode("geo", "receiver", run_init_scripts=False)
    grid = receiver.createNode("grid")
    pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((6, 4))
    grid.setRenderFlag(True)
    emitter = scene.createNode("geo", "mesh_emitter", run_init_scripts=False)
    box = emitter.createNode("box")
    box.parmTuple("size").set((0.8, 0.8, 0.8))
    box.setRenderFlag(True)
    pass_fixture.set_parameters(emitter, dict(tx=1.6, tz=0.8))
    object_properties.install(emitter)
    pass_fixture.parm(emitter, "cycles_lightgroup").set("Mesh")
    builder = materials.create_material(name="lightgroup_emission", shader="emission")
    # The default this test was written for (new nodes now take Blender's).
    builder.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
    emission = next(n for n in builder.children() if n.type().name() == "cycles::emission::1")
    emission.parmTuple("color").set((1, 0.25, 0.05))
    pass_fixture.parm(emission, "strength").set(2)
    pass_fixture.parm(emitter, "shop_materialpath").set(builder.path())
    lights = []
    for name, x, color in (("Key", -2, (1, 0, 0)), ("Fill", 2, (0, 1, 0))):
        light = scene.createNode(light_properties.LIGHT_TYPE, name)
        pass_fixture.set_parameters(
            light, dict(cycles_light_type="point", tx=x, tz=3, light_intensity=40, cycles_lightgroup=name)
        )
        light.parmTuple("cycles_light_color").set(color)
        lights.append(light)
    camera = hou.node("/obj").createNode("cam", "lightgroup_camera")
    pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=8))
    rop = hou.node("/out").createNode("cycles_ipr", "lightgroup_output")
    set_rop_devices(rop, device)
    pass_fixture.set_parameters(
        rop,
        dict(
            source_mode="objects",
            objectroot=scene.path(),
            camera=camera.path(),
            resx=96,
            resy=64,
            samples=128,
            material_kind="diffuse",
            use_material_assignments=1,
        ),
    )
    for descriptor in catalogue():
        parm = pass_fixture.parm(rop, "pass_" + descriptor["id"])
        if parm:
            parm.set(False)
    rop.parmTuple("basecolor").set((1, 1, 1))
    rop.parmTuple("background").set((0.06, 0.03, 0.015))
    pass_fixture.parm(rop, "background_lightgroup").set("World")
    names = ["Key", "Fill", "Mesh", "World", "Unused"]
    groups(rop, names)
    with ch.Session(
        device=device, width=96, height=64, samples=128, collect_diagnostics=True, passes=["combined"]
    ) as session:

        def render():
            return session.wait(session.apply(native_scene.capture(rop)))

        first = render()
        converted = session.status()["geometry_conversions"]
        split_matches(first, names)
        assert np.count_nonzero(plane(first, "lightgroup_Unused")) == 0
        assert np.count_nonzero(plane(first, "lightgroup_Key")[:, :, 1:]) == 0
        assert np.count_nonzero(plane(first, "lightgroup_Fill")[:, :, [0, 2]]) == 0
        assert plane(first, "lightgroup_Key").max() > 0.01 and plane(first, "lightgroup_Fill").max() > 0.01
        assert np.allclose(plane(first, "lightgroup_Mesh")[30:34, 65:69], (2, 0.5, 0.1), atol=2e-5)
        assert np.allclose(plane(first, "lightgroup_World")[0, 0], (0.06, 0.03, 0.015), atol=2e-5)
        info = session.frame_info()
        assert info["pass_metadata"]["lightgroup_Key"]["channels"] == ["R", "G", "B"]
        assert (
            info["pass_metadata"]["lightgroup_Key"]["color"]
            and not info["pass_metadata"]["lightgroup_Key"]["denoise_eligible"]
        )
        report["checks"].append(
            "Independent emitter/world RGB values; red/green analytic-light isolation; sum of all group RGB matches raw beauty; missing group zero"
        )
        pass_fixture.parm(lights[0], "cycles_lightgroup").set("Fill")
        moved = render()
        split_matches(moved, names)
        assert np.count_nonzero(plane(moved, "lightgroup_Key")) == 0
        assert np.allclose(
            plane(moved, "lightgroup_Fill"), plane(first, "lightgroup_Key") + plane(first, "lightgroup_Fill"), atol=2e-5
        )
        assert session.status()["geometry_conversions"] == converted
        pass_fixture.parm(emitter, "cycles_lightgroup").set("OtherMesh")
        groups(rop, names + ["OtherMesh"])
        renamed = render()
        split_matches(renamed, names + ["OtherMesh"])
        assert np.count_nonzero(plane(renamed, "lightgroup_Mesh")) == 0
        assert np.allclose(plane(renamed, "lightgroup_OtherMesh"), plane(first, "lightgroup_Mesh"), atol=2e-5)
        assert session.status()["geometry_conversions"] == converted

        def rejected(update):
            before = session.status()
            info = session.frame_info()
            try:
                session.wait(session.apply(update), timeout=15)
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("Invalid output configuration accepted: " + repr(update))
            after = session.status()
            assert after.get("committed_generation", after["generation"]) == before.get(
                "committed_generation", before["generation"]
            )
            assert session.frame_info()["generation"] == info["generation"]

        for invalid in (
            [""],
            ["bad name"],
            ["same", "same"],
            ["1name"],
            ["x" * 129],
            ["a\0b"],
            list("x" + str(i) for i in range(65)),
        ):
            rejected(dict(lightgroups=invalid))
        rejected(dict(aovs=[dict(name="lightgroup_Key", type="color")]))
        # Restore all source output settings before further partial edits.
        # The worker keeps the accepted image through rejection, while its
        # latest desired source can retain the invalid requested settings.
        repaired = render()
        split_matches(repaired, names + ["OtherMesh"])
        assert session.status()["geometry_conversions"] == converted
        report["checks"].append(
            "Live assignment retains geometry; invalid names/duplicates/colliding AOV leave the accepted scene intact and full-source repair restores output settings"
        )
        all_names = names + ["OtherMesh"]
        unregistered = session.wait(session.apply(dict(lightgroups=[])))
        assert set(unregistered["planes"]) == {"combined"}
        assert np.allclose(plane(unregistered, "combined"), plane(renamed, "combined"), atol=2e-5)
        restored = session.wait(session.apply(dict(lightgroups=list(reversed(all_names)))))
        split_matches(restored, all_names)
        raw = plane(restored, "lightgroup_Fill").copy()
        denoised = session.wait(session.apply(dict(denoising=dict(enabled=True, method="oidn", use_gpu=False))))
        assert np.allclose(plane(denoised, "lightgroup_Fill"), raw, atol=2e-5)
        session.wait(session.apply(dict(denoising=dict(enabled=False))))
        long_name = "Long_" + "x" * 123
        pass_fixture.parm(lights[0], "cycles_lightgroup").set(long_name)
        groups(rop, all_names + [long_name])
        generation = session.apply(native_scene.capture(rop) | dict(rendering=dict(samples=1048576)))
        session.set_preview_pass("lightgroup_" + long_name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = session.frame_info()
            if info and info["generation"] == generation and "lightgroup_" + long_name in info["passes"]:
                assert not info["complete"]
                break
            if session.status()["state"] == "failed":
                raise RuntimeError(session.status()["error"])
            time.sleep(0.005)
        else:
            raise TimeoutError("No progressive light-group preview")
        session.open_native_display("null:")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = session.frame_info()
            if (
                info
                and info["generation"] == generation
                and set(info["passes"]) == {"combined"} | {"lightgroup_" + name for name in all_names + [long_name]}
            ):
                session.present_native()
                break
            time.sleep(0.005)
        else:
            raise TimeoutError("No native multi-plane light-group delivery")
        session.close_native_display()
        session.set_preview_pass("combined")
        final = session.wait(session.apply(dict(rendering=dict(samples=16))))
        assert session.status()["geometry_conversions"] == converted
        target = destination / "lightgroups.exr"
        session.write_exr(target)
        image = OIIO.ImageInput.open(str(target))
        assert image
        spec = image.spec()
        data = image.read_image(format=OIIO.FLOAT)
        for name in all_names + [long_name]:
            channels = ["lightgroup_" + name + "." + c for c in "RGB"]
            assert (
                all(c in spec.channelnames for c in channels) and "lightgroup_" + name + ".A" not in spec.channelnames
            )
            assert np.array_equal(
                data[:, :, [spec.channelnames.index(c) for c in channels]],
                np.flipud(plane(final, "lightgroup_" + name)),
            )
        assert spec.get_string_attribute("compression") in ("zip", "zips")
        image.close()
        report["checks"].append(
            "Disabled/reordered outputs preserve beauty; group passes stay raw under denoising; long-name progressive preview/native planes; lossless RGB EXR round trip"
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    pass_fixture.set_parameters(rop, dict(trange=0, picture=str(destination / "native-rop.exr"), samples=16))
    hip = destination / "lightgroup-authoring.hiplc"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    hou.node("/out/lightgroup_output").render(frame_range=(1, 1), ignore_inputs=True)
    image = OIIO.ImageInput.open(str(destination / "native-rop.exr"))
    assert image
    assert (
        "lightgroup_OtherMesh.R" in image.spec().channelnames
        and "lightgroup_" + long_name + ".R" in image.spec().channelnames
    )
    image.close()
    report["checks"].append("Native ROP/SOHO batch and optional object/light parameters survive HIP reload")
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(path)
