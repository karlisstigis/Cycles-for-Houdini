# SPDX-License-Identifier: Apache-2.0
"""Real EXR codecs, selected guides only, exact data/IDs and live flag repairs."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path
import sys
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import _native, native_scene, output
from cycles_houdini.device_selection import set_devices

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
dest = artifacts_root() / "stage3/exr-policy"
dest.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "exr_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
cam = hou.node("/obj").createNode("cam", "exr_camera")
pass_fixture.parm(cam, "tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "exr_policy")
set_devices(rop, device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=cam.path(),
        resx=96,
        resy=64,
        samples=8,
        ipr_samples=8,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=1,
        ipr_denoiser="oidn",
        ipr_denoise_use_gpu=0,
        output_precision="half",
    ),
)
for entry in _native.pass_catalogue():
    parm = pass_fixture.parm(rop, "pass_" + entry["id"])
    if parm:
        parm.set(0)
checks = []


def read(path):
    image = oiio.ImageInput.open(str(path))
    assert image, oiio.geterror()
    spec = image.spec()
    pixels = image.read_image(format=oiio.FLOAT)
    image.close()
    return spec, pixels


with ch.Session(device=device, width=96, height=64, samples=8, interactive=True) as session:

    def render():
        return session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))

    frame = render()
    assert {"noisy_combined", "denoising_albedo", "denoising_normal"} <= set(frame["planes"])
    conversions = session.status()["geometry_conversions"]
    path = dest / "beauty.exr"
    modes = ("none", "rle", "zips", "zip", "piz", "pxr24", "b44", "b44a", "dwaa", "dwab")
    for codec in modes:
        pass_fixture.set_parameters(rop, dict(output_compression=codec, output_dwa_level=70))
        session.write_exr(path, output.options(rop, hou.time()))
        spec, pixels = read(path)
        assert set(spec.channelnames) == {"R", "G", "B", "A"}, (codec, spec.channelnames)
        assert spec.get_string_attribute("compression") == codec, (codec, spec.get_string_attribute("compression"))
        assert np.isfinite(pixels).all()
        if codec in ("dwaa", "dwab"):
            assert abs(spec.get_float_attribute("openexr:dwaCompressionLevel") - 70) < 0.01
    checks.append("All ten menu codecs produce actual EXRs; implicit denoising guides are absent")
    pass_fixture.parm(rop, "output_compression").set("zip")
    for selected in (True, False, True, False):
        pass_fixture.set_parameters(
            rop,
            {name: int(selected) for name in ("pass_noisy_combined", "pass_denoising_albedo", "pass_denoising_normal")},
        )
        render()
        session.write_exr(path, output.options(rop, hou.time()))
        spec, _ = read(path)
        for name in ("noisy_combined.R", "denoising_albedo.R", "denoising_normal.X"):
            assert (name in spec.channelnames) == selected, (selected, spec.channelnames)
        assert session.status()["geometry_conversions"] == conversions
    checks.append("Live pass toggles enable/disable export without removing denoising or reconverting geometry")
    pass_fixture.set_parameters(rop, dict(pass_depth=1, crypto_object=1, output_precision="half"))
    frame = render()
    for codec in ("zip", "piz"):
        pass_fixture.parm(rop, "output_compression").set(codec)
        session.write_exr(path, output.options(rop, hou.time()))
        spec, pixels = read(path)
        formats = dict(zip(spec.channelnames, map(str, spec.channelformats)))
        assert formats["R"] == "half" and formats["depth.Z"] == "float" and formats["CryptoObject00.R"] == "float"
        assert any(p.name.startswith("cryptomatte/") for p in spec.extra_attribs)
        for name in ("depth", "CryptoObject00"):
            plane = frame["planes"][name]
            source = np.frombuffer(plane["pixels"], np.float32).reshape(64, 96, plane["channels"])
            indices = [
                spec.channelnames.index(name + "." + channel)
                for channel in session.frame_info()["pass_metadata"][name]["channels"]
            ]
            assert np.array_equal(pixels[:, :, indices].view(np.uint32), np.flipud(source).copy().view(np.uint32)), (
                codec,
                name,
            )
    for codec in ("pxr24", "b44", "b44a", "dwaa", "dwab"):
        pass_fixture.parm(rop, "output_compression").set(codec)
        session.write_exr(path, output.options(rop, hou.time()))
        spec, _ = read(path)
        assert spec.get_string_attribute("compression") == codec
        assert {"depth.Z", "CryptoObject00.R"} <= set(spec.channelnames)
        assert any(p.name.startswith("cryptomatte/") for p in spec.extra_attribs)
        # Codec selection never changes the retained, uncompressed source.
        assert session.frame()["planes"]["CryptoObject00"]["pixels"] == frame["planes"]["CryptoObject00"]["pixels"]
    sentinel = path.read_bytes()
    try:
        session.write_exr(path, dict(compression="invalid-codec"))
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("An unknown codec was accepted")
    assert path.read_bytes() == sentinel
    checks.append(
        "ZIP/PIZ preserve exact data/IDs; explicit lossy codecs are honored on numeric/Cryptomatte channels; invalid codecs preserve completed files"
    )
    pass_fixture.set_parameters(rop, dict(pass_depth=0, crypto_object=0, output_compression="dwab"))
    render()
    session.write_exr(path, output.options(rop, hou.time()))
    assert set(read(path)[0].channelnames) == {"R", "G", "B", "A"}
    checks.append("Repair returns to denoised color-only DWAB in the same running session")

report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-exr-policy-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
