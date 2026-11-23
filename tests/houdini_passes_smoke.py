# SPDX-License-Identifier: Apache-2.0
"""Analytic pass values, lighting reconstruction, dynamic pass sets and SOHO EXR."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import sys
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import native_scene
from cycles_houdini.passes import catalogue, MANAGED, configuration

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/passes"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-passes-smoke.json"
report = dict(result="running", devices=[], checks=[])
devices = (sys.argv[sys.argv.index("--device") + 1],) if "--device" in sys.argv else ("CPU", ch.default_device())
descriptors = catalogue()
names = [p["id"] for p in descriptors if not p["managed"]]
expected = {
    "combined",
    "depth",
    "normal",
    "uv",
    "position",
    "sample_count",
    "object_id",
    "material_id",
    "roughness",
    "mist",
    "diffuse_direct",
    "diffuse_indirect",
    "diffuse_color",
    "glossy_direct",
    "glossy_indirect",
    "glossy_color",
    "transmission_direct",
    "transmission_indirect",
    "transmission_color",
    "volume_direct",
    "volume_indirect",
    "emission",
    "background",
    "ao",
    "shadow_catcher",
    "motion",
    "noisy_combined",
    "denoising_albedo",
    "denoising_normal",
    "denoising_depth",
}
assert set(names) == expected and {p["id"] for p in descriptors} == expected | MANAGED
assert {p["id"] for p in descriptors if p["denoise_eligible"]} == {"combined", "shadow_catcher"}
assert all(p["storage"] == "float32" for p in descriptors)
geo = hou.node("/obj").createNode("geo", "pass_receiver")
for child in geo.children():
    child.destroy()
box = geo.createNode("box", "native_box")
box.parmTuple("size").set((8, 8, 1))
box.setRenderFlag(True)
box.setDisplayFlag(True)
camera = hou.node("/obj").createNode("cam", "pass_camera")
pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def centre(frame, name):
    return plane(frame, name)[30:34, 46:50].mean(axis=(0, 1))


def exr(path, frame=None):
    image = OIIO.ImageInput.open(str(path))
    assert image is not None, OIIO.geterror()
    spec = image.spec()
    pixels = image.read_image(format=OIIO.FLOAT)
    assert len(spec.channelnames) == sum(len(p["channels"]) for p in descriptors if p["id"] in names)
    assert all(t == OIIO.TypeDesc("float") for t in spec.channelformats) or spec.format == OIIO.TypeDesc("float")
    for p in descriptors:
        if p["id"] not in names:
            continue
        channels = [c if p["id"] == "combined" else p["id"] + "." + c for c in p["channels"]]
        indices = [spec.channelnames.index(c) for c in channels]
        assert np.isfinite(pixels[:, :, indices]).all(), p["id"]
        if frame is not None:
            assert np.array_equal(pixels[:, :, indices], np.flipud(plane(frame, p["id"]))), p["id"]
    assert spec.get_string_attribute("compression") in ("zip", "zips")
    image.close()


try:
    for device in devices:
        checks = []
        with ch.Session(
            device=device, width=96, height=64, samples=64, interactive=False, passes=names, collect_diagnostics=True
        ) as session:
            base = dict(
                geometry=[ch.geometry_request(geo, box, material="surface")],
                camera_path=camera.path(),
                background=(0, 0, 0),
                rendering=dict(samples=64, mist_start=2, mist_depth=5, mist_falloff=1),
                materials=[dict(key="surface", kind="emission", color=(0.25, 0.5, 0.75), strength=2)],
            )
            frame = session.wait(session.apply(base))
            converted = session.status()["geometry_conversions"]
            assert set(frame["planes"]) == set(names)
            assert np.allclose(centre(frame, "combined"), (0.5, 1, 1.5, 1), atol=1e-6)
            assert np.allclose(centre(frame, "emission"), (0.5, 1, 1.5), atol=1e-6)
            assert np.allclose(centre(frame, "depth"), 4.5, atol=1e-4)
            assert np.allclose(centre(frame, "position")[2], 0.5, atol=1e-5)
            assert np.allclose(centre(frame, "normal"), (0, 0, 1), atol=1e-5)
            # Mist uses geometric camera-to-hit distance. The small central
            # orthographic patch has <0.002 variation from its 4.5-unit axis.
            assert np.allclose(centre(frame, "mist"), 0.5, atol=0.002), centre(frame, "mist")
            checks.append(
                dict(
                    name="Analytic emission, depth, position, normals and linear mist",
                    mist=centre(frame, "mist").tolist(),
                )
            )
            frame = session.wait(
                session.apply(dict(rendering=dict(samples=64, mist_start=2, mist_depth=5, mist_falloff=2)))
            )
            assert np.allclose(centre(frame, "mist"), 0.25, atol=0.002)
            color = (0.2, 0.4, 0.7)
            frame = session.wait(
                session.apply(
                    dict(
                        materials=[dict(key="surface", kind="diffuse", color=color, roughness=0)],
                        rendering=dict(samples=64, mist_start=2, mist_depth=5),
                        lights=[
                            dict(
                                key="key",
                                kind="point",
                                transform=(1, 0, 0, -4, 0, 1, 0, 0, 0, 0, 1, 5),
                                radius=0,
                                strength=(100, 100, 100),
                            )
                        ],
                    )
                )
            )
            assert np.allclose(centre(frame, "diffuse_color"), color, atol=1e-5)
            assert min(centre(frame, "diffuse_direct")) > 0
            assert np.allclose(centre(frame, "ao"), 1, atol=0.002)
            reconstructed = sum(
                (plane(frame, p + "_direct") + plane(frame, p + "_indirect")) * plane(frame, p + "_color")
                for p in ("diffuse", "glossy", "transmission")
            )
            reconstructed += sum(
                plane(frame, p) for p in ("volume_direct", "volume_indirect", "emission", "background")
            )
            error = float(np.abs(reconstructed - plane(frame, "combined")[:, :, :3]).max())
            assert error < 2e-5, error
            checks.append(dict(name="Lighting components reconstruct raw beauty", max_error=error))
            target = destination / (device.replace(":", "_").replace(" ", "_") + ".exr")
            session.write_exr(target)
            exr(target, frame)
            reference = frame
            denoised = session.wait(session.apply(dict(denoising=dict(enabled=True, method="oidn", use_gpu=False))))
            for name in names:
                assert np.isfinite(plane(denoised, name)).all(), name
                # Cycles explicitly supports denoising its catcher multiplier.
                # Raw lighting components and data channels remain invariant.
                if name not in ("combined", "shadow_catcher"):
                    assert np.allclose(plane(denoised, name), plane(reference, name), atol=2e-6, rtol=2e-5), name
            checks.append(
                dict(
                    name="Beauty/catcher denoising stays finite; lighting/data planes remain raw and EXR round-trips every float32 channel"
                )
            )
            minimal = session.wait(session.apply(dict(passes=["combined", "depth"], denoising=dict(enabled=False))))
            assert set(minimal["planes"]) == {"combined", "depth"}
            assert session.status()["geometry_conversions"] == converted
            restored = session.wait(session.apply(dict(passes=names)))
            assert set(restored["planes"]) == set(names)
            before = session.status()
            for invalid in ([], ["depth"], ["combined", "combined"], ["combined", "invalid"]):
                try:
                    session.apply(dict(passes=invalid))
                except (ValueError, RuntimeError):
                    pass
                else:
                    raise AssertionError("Invalid pass request accepted: " + str(invalid))
                after = session.status()
                assert after["generation"] == before["generation"] and after["state"] != "failed", (
                    invalid,
                    before,
                    after,
                )
                assert after["geometry_conversions"] == before["geometry_conversions"], (invalid, before, after)
                if "renderer_pid" in before:
                    assert after["renderer_pid"] == before["renderer_pid"], (invalid, before, after)
                retained = session.frame()
                assert retained["generation"] == restored["generation"] and retained["sequence"] == restored["sequence"]
                assert np.array_equal(plane(retained, "combined"), plane(restored, "combined"))
            checks.append(
                dict(
                    name="Pass changes retain converted geometry; invalid requests preserve the current generation",
                    conversions=converted,
                )
            )
        assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
        # Use the real native SOHO ROP to prove artist pass controls reach files.
        rop = hou.node("/out").createNode("cycles_ipr", "passes_output")
        output = destination / "passes-rop.exr"
        set_rop_devices(rop, device)
        pass_fixture.set_parameters(
            rop,
            dict(
                source_mode="sop",
                soppath=box.path(),
                camera=camera.path(),
                resx=96,
                resy=64,
                samples=8,
                material_kind="emission",
                emission_strength=2,
                picture=str(output),
            ),
        )
        rop.parmTuple("lightstrength").set((0, 0, 0))
        rop.parmTuple("basecolor").set((0.25, 0.5, 0.75))
        for name in names:
            if name != "combined":
                pass_fixture.parm(rop, "pass_" + name).set(1)
        assert set(configuration(rop, hou.time())) == set(names)
        assert "passes" in native_scene.capture(rop)
        rop.render(frame_range=(1, 1))
        exr(output)
        checks.append(dict(name="Native Passes tab reaches final SOHO multilayer EXR"))
        rop.destroy()
        report["devices"].append(dict(device=device, checks=checks))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2))
    print(str(report_path))
