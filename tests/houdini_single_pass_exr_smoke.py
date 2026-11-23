# SPDX-License-Identifier: Apache-2.0
"""Single-pass primary channels, shared layers and multipart preserve raw data."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import materials, native_scene, output, passes
from cycles_houdini.device_selection import set_devices
from multipart_fixture import read_parts

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
dest = artifacts_root() / "stage3/single-pass-exr"
dest.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "single_pass_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "single_pass_camera")
camera.parm("tz").set(5)
material = materials.create_material(name="single_pass_material", shader="emission")
# The default this test was written for (new nodes now take Blender's).
material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
writer = material.createNode("cycles::aov_output::1", "tint_writer")
writer.parm("name").set("toy_color")
writer.parmTuple("color").set((0.2, 0.4, 0.8))
scalar = material.createNode("cycles::aov_output::1", "scalar_writer")
scalar.setParms(dict(name="random_value", value=0.37))
assignment = geo.createNode("material")
assignment.setInput(0, sop)
assignment.setParms(dict(num_materials=1, shop_materialpath1=material.path()))
assignment.setRenderFlag(True)
rop = hou.node("/out").createNode("cycles_ipr", "single_pass_output")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=assignment.path(),
        camera=camera.path(),
        resx=64,
        resy=48,
        viewer_interactive=0,
        samples=8,
        adaptive_sampling=0,
        use_denoise=0,
        render_threads=2,
        picture=str(dest / "passes.$F4.exr"),
        output_compression="zip",
        output_precision="float",
        output_separate_passes=1,
        allowmotionblur=0,
    )
)
for kind in ("ao", "normal", "position", "depth", "uv", "motion"):
    passes.add(rop, kind)
color_index = passes.add(rop, "aov_color", source="toy_color", name="ToyColor")
value_index = passes.add(rop, "aov_value", source="random_value", name="Random")
passes.add(rop, "crypto_object", name="Masks", levels=2)


def read(path):
    image = oiio.ImageInput.open(str(path))
    assert image, oiio.geterror()
    try:
        spec = image.spec()
        pixels = image.read_image(0, 0, 0, spec.nchannels, oiio.FLOAT)
        assert pixels is not None, oiio.geterror()
        assert not image.seek_subimage(1, 0)
        return spec, pixels
    finally:
        image.close()


with ch.Session(device=device, width=64, height=48, samples=8, interactive=False) as session:
    frame = session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
    metadata = session.frame_info()["pass_metadata"]
    generation = session.status()["generation"]
    primary = {
        "combined": list("RGBA"),
        "ao": list("RGB"),
        "normal": list("RGB"),
        "position": list("RGB"),
        "depth": ["Y"],
        "uv": list("RGB"),
        "motion": list("RGBW"),
        "toy_color": list("RGBA"),
        "random_value": ["Y"],
    }

    def check_pixels(spec, pixels, selection, *, named):
        source = selection["source"]
        channels = metadata[source]["channels"]
        names = (
            [selection["layer"] + "." + c if selection["layer"] else c for c in channels] if named else primary[source]
        )
        assert set(names) <= set(spec.channelnames), (selection, spec.channelnames, names)
        indices = [list(spec.channelnames).index(name) for name in names]
        raw = frame["planes"][source]
        expected = np.flipud(np.frombuffer(raw["pixels"], np.float32).reshape(48, 64, raw["channels"]))
        actual = pixels[:, :, indices]
        assert np.array_equal(actual.view(np.uint32), expected.view(np.uint32)), source
        return names

    jobs = output.plan(rop, hou.time())
    output.write_outputs(session, jobs)
    for job in jobs:
        selections = job["options"]["passes"]
        assert len(selections) == 1
        selected = selections[0]
        crypto = selected["source"].startswith("Crypto")
        spec, pixels = read(job["filename"])
        names = check_pixels(spec, pixels, selected, named=crypto)
        assert set(spec.channelnames) == set(names)
        if selected["source"] == "toy_color":
            assert set(spec.channelnames) == set("RGBA")
            assert spec.alpha_channel == list(spec.channelnames).index("A")
            np.testing.assert_allclose(
                pixels[24, 32, [list(spec.channelnames).index(c) for c in "RGBA"]], (0.2, 0.4, 0.8, 1), atol=1e-6
            )
        if selected["source"] == "normal":
            assert set(spec.channelnames) == set("RGB") and "Y" not in spec.channelnames
            np.testing.assert_allclose(
                pixels[24, 32, [list(spec.channelnames).index(c) for c in "RGB"]], (0, 0, 1), atol=1e-6
            )
        if selected["source"] == "motion":
            assert "A" not in spec.channelnames and spec.alpha_channel == -1
        if crypto:
            assert set(spec.channelnames) == {"Masks00." + c for c in "RGBA"}
            assert any(p.name.endswith("/name") and p.value == "Masks" for p in spec.extra_attribs)

    # Route two ordinary passes into one separate file. Determine naming from
    # the final file contents, not from either row's Separate File setting.
    shared = str(dest / "shared.exr")
    rop.setParms({"pass_path" + str(color_index): shared, "pass_path" + str(value_index): shared})
    jobs = output.plan(rop, hou.time())
    output.write_outputs(session, jobs)
    job = next(job for job in jobs if Path(job["filename"]) == Path(shared))
    assert len(job["options"]["passes"]) == 2
    spec, pixels = read(shared)
    for selected in job["options"]["passes"]:
        check_pixels(spec, pixels, selected, named=True)
    assert set(spec.channelnames) == {"ToyColor." + c for c in "RGBA"} | {"Random.X"}

    rop.setParms(dict(output_separate_passes=0))
    jobs = output.plan(rop, hou.time())
    assert len(jobs) == 1
    output.write_outputs(session, jobs)
    spec, pixels = read(jobs[0]["filename"])
    for selected in jobs[0]["options"]["passes"]:
        check_pixels(spec, pixels, selected, named=True)
    assert "ToyColor.R" in spec.channelnames and "normal.X" in spec.channelnames

    rop.parm("output_multipart").set(1)
    jobs = output.plan(rop, hou.time())
    output.write_outputs(session, jobs)
    parts = read_parts(jobs[0]["filename"])
    for selected in jobs[0]["options"]["passes"]:
        part = parts[selected["part"]]
        # read_parts supplies channels separately, retaining each part's name.
        spec = oiio.ImageSpec()
        spec.channelnames = part["channels"]
        check_pixels(spec, part["pixels"], selected, named=True)

    # Even a one-part multipart file retains the authored pass prefix.
    selected = dict(source="toy_color", layer="ToyColor", part="ToyColor", precision="float")
    single_part = dest / "one-part.exr"
    session.write_exr(single_part, dict(multipart=True, compression="zip", passes=[selected]))
    spec, pixels = read(single_part)
    assert set(spec.channelnames) == {"ToyColor." + c for c in "RGBA"}
    assert spec.get_string_attribute("oiio:subimagename") == "ToyColor"
    check_pixels(spec, pixels, selected, named=True)
    assert session.status()["generation"] == generation
    assert session.frame()["planes"]["toy_color"]["pixels"] == frame["planes"]["toy_color"]["pixels"]

report = dict(
    result="passed",
    device=device,
    checks=[
        "Separate color AOV is primary RGBA with its own recognized coverage alpha",
        "Standalone AO/vectors use primary RGB, scalar data uses Y, motion W remains data; float pixels bit-exact",
        "One-layer Cryptomatte retains numbered channel names and manifest",
        "Explicitly shared files and multilayer/multipart outputs retain pass prefixes and raw values",
        "One-part multipart file retains its prefix; saving modes never restart rendering or mutate the frame",
    ],
)
(dest / "houdini-single-pass-exr-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
