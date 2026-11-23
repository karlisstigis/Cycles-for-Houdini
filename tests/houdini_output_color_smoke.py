# SPDX-License-Identifier: Apache-2.0
"""EXR color conversion vs independent OCIO, including exact raw pass bits."""

from support.paths import artifacts_root

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
import sys
from support.devices import fixture_device
from pathlib import Path
import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, output
from cycles_houdini.queued import WorkerSession

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/output-color"
dest.mkdir(parents=True, exist_ok=True)
device = fixture_device(sys.argv)
geo = hou.node("/obj").createNode("geo", "color_subject", run_init_scripts=False)
box = geo.createNode("box")
camera = hou.node("/obj").createNode("cam")
pass_fixture.parm(camera, "tz").set(4)
rop = hou.node("/out").createNode("cycles_ipr")
set_rop_devices(rop, device)
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="sop",
        soppath=box.path(),
        camera=camera.path(),
        resx=64,
        resy=48,
        samples=16,
        viewer_interactive=0,
        adaptive_sampling=0,
        use_denoise=0,
        transparent_background=1,
        pass_motion=1,
        allowmotionblur=0,
        crypto_object=1,
        pass_noisy_combined=1,
        pass_denoising_albedo=1,
        pass_denoising_normal=1,
        pass_denoising_depth=1,
        pass_diffuse_color=1,
        pass_emission=1,
        pass_ao=1,
        pixel_filter="blackman_harris",
        filter_width=1.5,
    ),
)
rop.parmTuple("background").set((0.2, 0.4, 0.7))
policy = ch.color_policy()
config = ocio.GetCurrentConfig()
targets = ["ACES2065-1", "ACEScct"]
assert all(t in pass_fixture.parm(rop, "output_color_space").menuItems() for t in targets)
assert all(not config.getColorSpace(t).isData() for t in policy["output_spaces"])


def read(path):
    f = oiio.ImageInput.open(str(path))
    assert f, oiio.geterror()
    spec = f.spec()
    pixels = f.read_image(format=oiio.FLOAT)
    f.close()
    return spec, pixels


checks = []
for kind, cls in [("native", ch.Session), ("worker", WorkerSession)]:
    execution = "sync" if kind == "native" else "worker"
    with cls(device=device, width=64, height=48, samples=16, interactive=True, execution=execution) as session:
        assert session.execution == execution
        frame = session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
        metadata = session.frame_info()["pass_metadata"]
        pass_fixture.set_parameters(rop, dict(output_color_override=0, output_precision="float"))
        raw = dest / (kind + "-raw.exr")
        session.write_exr(raw, output.options(rop, hou.time()))
        raw_spec, raw_pixels = read(raw)
        alpha = raw_pixels[:, :, raw_spec.channelnames.index("A")]
        assert np.any((alpha > 0) & (alpha < 1)) and np.any(alpha == 0)
        for target in targets:
            pass_fixture.set_parameters(rop, dict(output_color_override=1, output_color_space=target))
            path = dest / (kind + "-" + target + ".exr")
            session.write_exr(path, output.options(rop, hou.time()))
            spec, pixels = read(path)
            assert spec.get_string_attribute("oiio:ColorSpace") == target
            assert spec.get_string_attribute("cycles_houdini:render_working_space") == frame["working_space"]
            processor = config.getProcessor(frame["working_space"], target).getDefaultCPUProcessor()
            for name, desc in metadata.items():
                ids = [spec.channelnames.index(c if name == "combined" else name + "." + c) for c in desc["channels"]]
                before = np.ascontiguousarray(raw_pixels[:, :, ids])
                after = np.ascontiguousarray(pixels[:, :, ids])
                if desc["color"]:
                    rgb = before[:, :, :3].copy()
                    if len(ids) == 4:
                        a = before[:, :, 3]
                        nonzero = a != 0
                        rgb[nonzero] /= a[nonzero, None]
                    rgb = np.ascontiguousarray(rgb)
                    processor.applyRGB(rgb)
                    if len(ids) == 4:
                        rgb[nonzero] *= a[nonzero, None]
                        assert np.array_equal(after[:, :, 3].view(np.uint32), a.view(np.uint32))
                    assert np.allclose(after[:, :, :3], rgb, atol=3e-6, rtol=3e-5), (kind, target, name)
                else:
                    assert np.array_equal(before.view(np.uint32), after.view(np.uint32)), (kind, target, name)
            assert not np.array_equal(raw_pixels[:, :, :3], pixels[:, :, :3])
            for p in raw_spec.extra_attribs:
                if p.name.startswith("cryptomatte/"):
                    assert spec.get_string_attribute(p.name) == raw_spec.get_string_attribute(p.name)
            checks.append(dict(execution=kind, target=target, raw_passes_bit_exact=True, alpha_bit_exact=True))
        pass_fixture.parm(rop, "output_precision").set("half")
        half = dest / (kind + "-half.exr")
        session.write_exr(half, output.options(rop, hou.time()))
        half_spec, half_pixels = read(half)
        for name, desc in metadata.items():
            ids = [spec.channelnames.index(c if name == "combined" else name + "." + c) for c in desc["channels"]]
            expected = (
                pixels[:, :, ids].astype(np.float16).astype(np.float32) if name == "combined" else pixels[:, :, ids]
            )
            assert np.array_equal(half_pixels[:, :, ids].view(np.uint32), expected.view(np.uint32)), (
                kind,
                name,
                "half",
            )
        pass_fixture.parm(rop, "output_precision").set("float")
        # Invalid targets/config generations cannot replace an existing file.
        previous = raw.read_bytes()
        raw_space = next(s.getName() for s in config.getColorSpaces() if s.isData())
        for opts in [
            dict(color_space="missing-space"),
            dict(color_space=raw_space),
            dict(color_space=targets[0], color_configuration="old-config"),
        ]:
            try:
                session.write_exr(raw, opts)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Invalid output color request succeeded")
            assert raw.read_bytes() == previous
        # Saving never mutates retained source pixels or their metadata.
        current = session.frame()
        for name, p in frame["planes"].items():
            assert current["planes"][name]["pixels"] == p["pixels"]
        pass_fixture.parm(rop, "output_color_override").set(0)
        session.write_exr(raw, output.options(rop, hou.time()))
        _, again = read(raw)
        assert np.array_equal(raw_pixels.view(np.uint32), again.view(np.uint32))

# Actual ROP final render uses the same override path.
pass_fixture.set_parameters(
    rop, dict(output_color_override=1, output_color_space=targets[1], picture=str(dest / "batch.exr"))
)
rop.render()
spec, _ = read(dest / "batch.exr")
assert spec.get_string_attribute("oiio:ColorSpace") == targets[1]
report = dict(result="passed", checks=checks, batch_override=True)
(dest / "houdini-output-color-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
