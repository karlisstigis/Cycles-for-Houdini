# SPDX-License-Identifier: Apache-2.0
"""Independent constant AOV expectations, native authoring, live updates and EXRs."""

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
from cycles_houdini import materials, native_scene
from cycles_houdini.passes import aov_configuration

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/passes/aovs"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-aov-smoke.json"
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
report = dict(result="running", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=[])


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def centre(frame, name):
    return plane(frame, name)[28:36, 44:52].mean(axis=(0, 1))


def aovs(rop, entries):
    pass_fixture.parm(rop, "aov_count").set(len(entries))
    for index, (name, type) in enumerate(entries, 1):
        pass_fixture.set_parameters(rop, {"aov_name" + str(index): name, "aov_type" + str(index): type})
    return aov_configuration(rop, hou.time())


def material(builder):
    return dict(key=builder.path(), kind="graph", graph=materials.material_graph(builder))


try:
    geo = hou.node("/obj").createNode("geo", "aov_receiver")
    for child in geo.children():
        child.destroy()
    box = geo.createNode("box")
    box.parmTuple("size").set((8, 8, 1))
    box.setRenderFlag(True)
    box.setDisplayFlag(True)
    camera = hou.node("/obj").createNode("cam", "aov_camera")
    pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
    builder = materials.create_material(name="aov_surface", shader="emission")
    # The default this test was written for (new nodes now take Blender's).
    builder.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
    tint = builder.createNode("cycles::aov_output::1", "tint_writer")
    pass_fixture.set_parameters(tint, dict(name="PaintTint", value=0.13))
    tint.parmTuple("color").set((-0.2, 0.5, 2))
    mask = builder.createNode("cycles::aov_output::1", "mask_writer")
    pass_fixture.set_parameters(mask, dict(name="Mask", value=0.37))
    mask.parmTuple("color").set((0.9, 0.8, 0.7))
    later = builder.createNode("cycles::aov_output::1", "later_writer")
    pass_fixture.set_parameters(later, dict(name="Later", value=0.71))
    assignment = geo.createNode("material")
    assignment.setInput(0, box)
    pass_fixture.set_parameters(assignment, dict(num_materials=1, shop_materialpath1=builder.path()))
    assignment.setRenderFlag(True)
    assignment.setDisplayFlag(True)
    rop = hou.node("/out").createNode("cycles_ipr", "aov_output")
    set_rop_devices(rop, device)
    pass_fixture.set_parameters(
        rop, dict(soppath=assignment.path(), camera=camera.path(), resx=96, resy=64, samples=16)
    )
    requested = aovs(rop, [("PaintTint", "color"), ("Mask", "value"), ("Missing", "value")])
    hou.hipFile.save(str(destination / "aov-authoring.hiplc"))
    assert tint.outputNames() == () and set(tint.inputNames()) == {"color", "value"}
    assert native_scene.capture(rop)["aovs"] == requested
    with ch.Session(
        device=device, width=96, height=64, samples=16, collect_diagnostics=True, passes=["combined"]
    ) as session:
        base = dict(
            geometry=[ch.geometry_request(geo, assignment, material=builder.path())],
            camera_path=camera.path(),
            background=(0, 0, 0),
            aovs=requested,
            materials=[material(builder)],
        )
        frame = session.wait(session.apply(base))
        conversions = session.status()["geometry_conversions"]
        assert np.allclose(centre(frame, "PaintTint"), (-0.2, 0.5, 2, 1), atol=1e-6)
        assert np.allclose(centre(frame, "Mask"), 0.37, atol=1e-6)
        assert not np.any(plane(frame, "Missing"))
        reference_tint = plane(frame, "PaintTint").copy()
        scalar = session.display_image(**{"pass": "Mask"}, exposure=0)
        assert (
            scalar["rgba"]
            == session.display_image(**{"pass": "Mask"}, exposure=3, display="invalid", view="invalid")["rgba"]
        )
        assert (
            session.display_image(**{"pass": "PaintTint"}, raw=True, exposure=-1)["rgba"]
            != session.display_image(**{"pass": "PaintTint"}, raw=True)["rgba"]
        )
        report["checks"].append(
            "Independent constant RGBA/scalar values, missing writer zero, HDR and raw scalar display"
        )
        denoised = session.wait(session.apply(dict(denoising=dict(enabled=True, method="oidn", use_gpu=False))))
        assert np.allclose(plane(denoised, "PaintTint"), reference_tint, atol=1e-6)
        assert np.allclose(centre(denoised, "Mask"), 0.37, atol=1e-6)
        # No writer graph is resent here: changing types must rebuild its saved original inputs.
        changed = [dict(name="PaintTint", type="value"), dict(name="Later", type="value")]
        frame = session.wait(session.apply(dict(aovs=changed, denoising=dict(enabled=False))))
        assert np.allclose(centre(frame, "PaintTint"), 0.13, atol=1e-6)
        assert np.allclose(centre(frame, "Later"), 0.71, atol=1e-6)
        assert "Mask" not in frame["planes"]
        frame = session.wait(session.apply(dict(aovs=requested)))
        assert np.allclose(centre(frame, "PaintTint"), (-0.2, 0.5, 2, 1), atol=1e-6)
        frame = session.wait(session.apply(dict(aovs=[])))
        assert set(frame["planes"]) == {"combined"}
        frame = session.wait(session.apply(dict(aovs=requested)))
        assert np.allclose(centre(frame, "Mask"), 0.37, atol=1e-6)
        assert session.status()["geometry_conversions"] == conversions
        report["checks"].append(
            "Rename/type/disable/re-enable update saved shaders without reconverting geometry; denoising leaves AOVs raw"
        )
        before = session.status()["generation"]
        invalid = [
            dict(name=name, type="color")
            for name in ("", "combined", "noisy_combined", "motion", "R", "foo.bar", "1mask", "a\0b", "x" * 129)
        ]
        for entries in ([item] for item in invalid):
            try:
                session.apply(dict(aovs=entries))
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("Invalid AOV accepted: " + repr(entries))
            assert session.status()["generation"] == before
        for entries in (
            [dict(name="same", type="color"), dict(name="same", type="value")],
            [dict(name="valid", type="unknown")],
            [dict(name="valid", type="color", typo=1)],
        ):
            try:
                session.apply(dict(aovs=entries))
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("Invalid AOV accepted: " + repr(entries))
        # Long valid names must also traverse the worker preview protocol.
        long_name = "Long_" + "x" * 90
        pass_fixture.parm(mask, "name").set(long_name)
        frame = session.wait(
            session.apply(dict(aovs=[dict(name=long_name, type="value")], materials=[material(builder)]))
        )
        session.set_preview_pass(long_name)
        assert np.allclose(centre(frame, long_name), 0.37, atol=1e-6)
        session.set_preview_pass("combined")
        pass_fixture.parm(mask, "name").set("Mask")
        generation = session.apply(dict(aovs=requested, materials=[material(builder)], rendering=dict(samples=1048576)))
        session.set_preview_pass("Mask")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = session.frame_info()
            if info and info["generation"] == generation and set(info["passes"]) == {"combined", "Mask"}:
                assert not info["complete"]
                break
            if session.status()["state"] == "failed":
                raise RuntimeError(session.status()["error"])
            time.sleep(0.005)
        else:
            raise TimeoutError("No progressive scalar AOV")
        assert session.status()["geometry_conversions"] == conversions
        session.open_native_display("null:")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = session.frame_info()
            if (
                info
                and info["generation"] == generation
                and set(info["passes"]) == {"combined", "PaintTint", "Mask", "Missing"}
            ):
                session.present_native()
                break
            time.sleep(0.005)
        else:
            raise TimeoutError("No native multi-plane shader AOV delivery")
        session.close_native_display()
        session.set_preview_pass("combined")
        pass_fixture.parm(mask, "value").setExpression("$F / 10", hou.exprLanguage.Hscript)
        for frame_number in (1, 2, 3):
            hou.setFrame(frame_number)
            frame = session.wait(session.apply(dict(materials=[material(builder)], rendering=dict(samples=16))))
            assert np.allclose(centre(frame, "Mask"), frame_number / 10, atol=1e-6)
            assert session.status()["geometry_conversions"] == conversions
        target = destination / "custom-aovs.exr"
        session.write_exr(target)
        image = OIIO.ImageInput.open(str(target))
        assert image is not None, OIIO.geterror()
        spec = image.spec()
        pixels = image.read_image(format=OIIO.FLOAT)
        assert set(spec.channelnames) == {
            "R",
            "G",
            "B",
            "A",
            "PaintTint.R",
            "PaintTint.G",
            "PaintTint.B",
            "PaintTint.A",
            "Mask.X",
            "Missing.X",
        }
        assert spec.format == OIIO.TypeDesc("float") or all(t == OIIO.TypeDesc("float") for t in spec.channelformats)
        for name, channels in (("PaintTint", ["R", "G", "B", "A"]), ("Mask", ["X"]), ("Missing", ["X"])):
            indices = [spec.channelnames.index(name + "." + c) for c in channels]
            assert np.array_equal(pixels[:, :, indices], np.flipud(plane(frame, name)))
        assert spec.get_string_attribute("compression") in ("zip", "zips")
        image.close()
        report["checks"].append(
            "Progressive selected-only and native multi-plane delivery; animated VOP values; float32 lossless EXR round trip"
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    # Exercise the artist's ROP/SOHO final path and saved multiparm parameters.
    saved = destination / "aov-authoring-current.hiplc"
    hou.hipFile.save(str(saved))
    rop_path = rop.path()
    hou.hipFile.load(str(saved), suppress_save_prompt=True)
    rop = hou.node(rop_path)
    assert aov_configuration(rop, hou.time()) == requested
    output = destination / "rop-aovs.exr"
    pass_fixture.parm(rop, "picture").set(str(output))
    rop.render(frame_range=(3, 3))
    image = OIIO.ImageInput.open(str(output))
    assert image is not None, OIIO.geterror()
    spec = image.spec()
    pixels = image.read_image(format=OIIO.FLOAT)
    assert {"Mask.X", "PaintTint.R", "PaintTint.G", "PaintTint.B", "PaintTint.A", "Missing.X"} <= set(spec.channelnames)
    assert np.allclose(pixels[28:36, 44:52, spec.channelnames.index("Mask.X")], 0.3, atol=1e-6)
    image.close()
    report["checks"].append("Native ROP/SOHO final output and HIP save/reload retain the AOV list")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2))
    print(str(report_path))
