# SPDX-License-Identifier: Apache-2.0
"""4K many-pass EXR encode timing/RSS and independent band-by-band validation.

Run against old/new isolated installs with different --report destinations.
Hashes describe decoded pixel bits, so codec buffering need not be identical.
"""

import argparse
import gc
import hashlib
import json
from pathlib import Path
import threading
import time
import hou
import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio
import pass_fixtures as fixture
import cycles_houdini as ch
from cycles_houdini import native_scene, output
from cycles_houdini.device_selection import set_devices

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--width", type=int, default=3840)
p.add_argument("--height", type=int, default=2160)
p.add_argument("--report", type=Path, required=True)
args = p.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "band_subject", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
geo.parm("rz").set(17)  # Oblique silhouette supplies fractional multisample coverage.
cam = hou.node("/obj").createNode("cam", "band_camera")
cam.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "band_output")
set_devices(rop, args.device)
fixture.set_parameters(
    rop,
    dict(
        source_mode="sop",
        soppath=box.path(),
        camera=cam.path(),
        resx=args.width,
        resy=args.height,
        samples=4,
        ipr_samples=4,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        transparent_background=1,
        output_precision="float",
        output_compression="zip",
        pass_depth=1,
        pass_normal=1,
        pass_position=1,
        pass_uv=1,
        pass_diffuse_color=1,
        pass_emission=1,
        pass_motion=1,
        pass_ao=1,
        crypto_object=1,
        allowmotionblur=0,
    ),
)
rop.parmTuple("background").set((0.2, 0.4, 0.7))
runs = []
with ch.Session(
    device=args.device, width=args.width, height=args.height, samples=4, interactive=False, execution="sync"
) as session:
    generation = session.apply(native_scene.capture(rop), at_time=hou.time())
    started = time.monotonic()
    while True:
        status = session.status()
        info = session.frame_info()
        assert status["state"] != "failed", status
        if info and info["complete"] and info["generation"] == generation:
            break
        assert time.monotonic() - started < 240, status
        time.sleep(0.05)
    metadata = info["pass_metadata"]
    for target in ("", "ACEScct"):
        options = output.options(rop, hou.time())
        if target:
            options.update(color_space=target, color_configuration=ch.color_policy()["configuration_id"])
        path = args.report.parent / ("bands-" + (target or "raw") + ".exr")
        gc.collect()
        baseline = ch.process_memory()["host"]
        samples = []
        stop = threading.Event()

        def sample():
            while not stop.wait(0.025):
                samples.append(ch.process_memory()["host"])

        sampler = threading.Thread(target=sample)
        sampler.start()
        started = time.perf_counter()
        try:
            session.write_exr(path, options)
        finally:
            elapsed = time.perf_counter() - started
            stop.set()
            sampler.join()
        after = ch.process_memory()["host"]
        samples.extend((baseline, after))
        measured = dict(
            seconds=elapsed,
            working_set_before=baseline["working_set_bytes"],
            peak_sampled_working_set=max(s["working_set_bytes"] for s in samples),
            peak_sampled_private_commit=max(s["private_commit_bytes"] for s in samples),
            private_commit_before=baseline["private_commit_bytes"],
            file_bytes=path.stat().st_size,
        )
        args.report.write_text(
            json.dumps(
                dict(
                    result="verifying",
                    width=args.width,
                    height=args.height,
                    runs=runs,
                    current_measurement=dict(target=target or "raw", **measured),
                ),
                indent=2,
            )
        )
        # Verification deliberately follows measurement: Python copies and
        # independent OCIO work are excluded from encode RSS/time.
        frame = session.frame()
        image = oiio.ImageInput.open(str(path))
        assert image, oiio.geterror()
        spec = image.spec()
        digest = hashlib.sha256()
        covered_fractional = False
        processor = (
            ocio.GetCurrentConfig().getProcessor(frame["working_space"], target).getDefaultCPUProcessor()
            if target
            else None
        )
        try:
            for first in range(0, spec.height, 32):
                rows = min(32, spec.height - first)
                pixels = image.read_scanlines(spec.y + first, spec.y + first + rows, 0, 0, spec.nchannels, oiio.FLOAT)
                assert pixels is not None, oiio.geterror()
                pixels = np.asarray(pixels).reshape(rows, spec.width, spec.nchannels)
                digest.update(pixels.tobytes())
                for selected in options["passes"]:
                    name = selected["source"]
                    desc = metadata[name]
                    plane = frame["planes"][name]
                    channels = desc["channels"]
                    layer = selected.get("layer", "") or (name if name != "combined" else "")
                    ids = [spec.channelnames.index(layer + "." + c if layer else c) for c in channels]
                    source = np.frombuffer(plane["pixels"], np.float32).reshape(spec.height, spec.width, len(channels))
                    expected = np.ascontiguousarray(source[spec.height - first - rows : spec.height - first][::-1])
                    actual = np.ascontiguousarray(pixels[:, :, ids])
                    if name == "combined":
                        a = expected[:, :, 3]
                        covered_fractional |= bool(np.any((a > 0) & (a < 1)))
                        assert np.array_equal(actual[:, :, 3].view(np.uint32), a.view(np.uint32))
                    if processor and desc["color"]:
                        rgb = expected[:, :, :3].copy()
                        if len(channels) == 4:
                            a = expected[:, :, 3]
                            nonzero = a != 0
                            rgb[nonzero] /= a[nonzero, None]
                        rgb = np.ascontiguousarray(rgb)
                        processor.applyRGB(rgb)
                        if len(channels) == 4:
                            rgb[nonzero] *= a[nonzero, None]
                        assert np.allclose(actual[:, :, :3], rgb, atol=3e-6, rtol=3e-5), (target, name, first)
                    else:
                        assert np.array_equal(actual.view(np.uint32), expected.view(np.uint32)), (target, name, first)
            assert covered_fractional, "Missing fractional edge alpha"
            crypto = {a.name: a.value for a in spec.extra_attribs if a.name.startswith("cryptomatte/")}
            assert any(k.endswith("/manifest") for k in crypto), crypto
            runs.append(
                dict(
                    target=target or "raw",
                    channels=list(spec.channelnames),
                    decoded_sha256=digest.hexdigest(),
                    crypto=crypto,
                    **measured,
                )
            )
        finally:
            image.close()
        del frame
        gc.collect()
        args.report.write_text(
            json.dumps(dict(result="running", width=args.width, height=args.height, runs=runs), indent=2)
        )
args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            width=args.width,
            height=args.height,
            scope="4K scanline ZIP; all selected source planes/alpha/IDs bit-exact, ACEScct independently checked in 32-row bands",
            runs=runs,
        ),
        indent=2,
    )
)
print(args.report)
