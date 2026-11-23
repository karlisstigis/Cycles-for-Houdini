# SPDX-License-Identifier: Apache-2.0
"""One native render: Beauty DWAB, grouped data/Cryptomatte ZIP and renamed PIZ."""

from support.paths import artifacts_root
import hashlib
import json
import os
from pathlib import Path
import sys
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, output, passes
from cycles_houdini.device_selection import set_devices, refresh

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
dest = artifacts_root() / "stage3/pass-outputs"
dest.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "routed_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
cam = hou.node("/obj").createNode("cam", "routed_camera")
cam.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "routed_outputs")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=cam.path(),
        resx=96,
        resy=64,
        samples=16,
        adaptive_sampling=0,
        use_denoise=1,
        denoiser="oidn",
        denoise_use_gpu=0,
        render_threads=2,
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        output_precision="half",
        output_compression="dwab",
        output_dwa_level=70,
        picture=str(dest / "beauty.$F4.exr"),
    )
)
rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
assert rop.parm("pass_depth") is None and rop.parm("aov_count") is None and rop.parm("crypto_object") is None
assert rop.parmTemplateGroup().find("pass_type#").menuType() == hou.menuType.Normal
stored = rop.parmTemplateGroup()
stored.append(hou.ToggleParmTemplate("pass_depth", "Retired Depth"))
stored.append(hou.FloatParmTemplate("artist_spare", "Artist Spare", 1, default_value=(7,)))
rop.setParmTemplateGroup(stored)
refresh(rop)
assert rop.parm("pass_depth") is None and rop.evalParm("artist_spare") == 7
zip_index = passes.add(
    rop,
    "crypto_object",
    name="masksZIP",
    levels=6,
    separate=True,
    path=str(dest / "masksZIP.$F4.exr"),
    compression="zip",
)
piz_index = passes.add(
    rop,
    "crypto_object",
    name="masksPIZ",
    levels=6,
    separate=True,
    path=str(dest / "masksPIZ.$F4.exr"),
    compression="piz",
)
normal_index = passes.add(
    rop, "normal", name="N", separate=True, path=str(dest / "masksZIP.$F4.exr"), compression="zip"
)
rop.updateParmStates()
assert rop.parm("pass_levels" + str(zip_index)).isHidden() is False
assert rop.parm("pass_levels" + str(normal_index)).isHidden()
assert rop.parm("pass_source" + str(normal_index)).isHidden()
assert len(output.plan(rop, hou.time())) == 3
rop.render(frame_range=(1, 1))
summary = rop.cachedUserData(final_render._summary_key)
assert summary["completed_frames"] == summary["created_sessions"] == summary["last_generation"] == 1, summary
assert len(summary["last_outputs"]) == 3 and not summary["error"], summary


def read(name):
    path = dest / (name + ".0001.exr")
    image = oiio.ImageInput.open(str(path))
    assert image, oiio.geterror()
    spec = image.spec()
    pixels = image.read_image(format=oiio.FLOAT)
    image.close()
    return path, spec, pixels


beauty_path, beauty, beauty_pixels = read("beauty")
zip_path, zip_spec, zip_pixels = read("masksZIP")
piz_path, piz_spec, piz_pixels = read("masksPIZ")
assert set(beauty.channelnames) == {"R", "G", "B", "A"}, beauty.channelnames
assert not any(p.name.startswith("cryptomatte/") for p in beauty.extra_attribs)
assert beauty.get_string_attribute("compression") == "dwab"
np.testing.assert_allclose(
    beauty_pixels[32, 48, [beauty.channelnames.index(c) for c in ("R", "G", "B")]], (0.2, 0.4, 0.8), atol=0.025, rtol=0
)
assert all(str(f) == "half" for f in beauty.channelformats) or str(beauty.format) == "half"
for name, spec in (("masksZIP", zip_spec), ("masksPIZ", piz_spec)):
    assert spec.get_string_attribute("compression") == ("zip" if name == "masksZIP" else "piz")
    assert all(str(f) == "float" for f in spec.channelformats) or str(spec.format) == "float"
    metadata = {p.name: p.value for p in spec.extra_attribs if p.name.startswith("cryptomatte/")}
    assert list(value for key, value in metadata.items() if key.endswith("/name")) == [name], metadata
    assert any(key.endswith("/manifest") for key in metadata)
assert all(name in zip_spec.channelnames for name in ("N.X", "N.Y", "N.Z"))
assert all(not name.startswith("N.") for name in piz_spec.channelnames)
for layer in range(3):
    a = [zip_spec.channelnames.index(f"masksZIP{layer:02}." + c) for c in "RGBA"]
    b = [piz_spec.channelnames.index(f"masksPIZ{layer:02}." + c) for c in "RGBA"]
    assert np.array_equal(zip_pixels[:, :, a].view(np.uint32), piz_pixels[:, :, b].view(np.uint32))
before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (beauty_path, zip_path, piz_path)}
rop.parm("pass_path" + str(piz_index)).set(str(zip_path))
try:
    output.plan(rop, hou.time())
except ValueError as error:
    assert "matching compression" in str(error), error
else:
    raise AssertionError("Conflicting compression for one file was accepted")
assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in before.items())
rop.parm("pass_path" + str(piz_index)).set(str(dest / "masksPIZ.$F4.exr"))
assert len(output.plan(rop, hou.time())) == 3
rop.parm("output_existing").set("skip")
rop.render(frame_range=(1, 1))
skipped = rop.cachedUserData(final_render._summary_key)
assert skipped["skipped_frames"] == 1 and skipped["created_sessions"] == 0 and len(skipped["last_outputs"]) == 3, (
    skipped
)
piz_path.unlink()
rop.render(frame_range=(1, 1))
resumed = rop.cachedUserData(final_render._summary_key)
assert resumed["completed_frames"] == resumed["created_sessions"] == 1 and resumed["skipped_frames"] == 0, resumed
assert beauty_path.read_bytes() and hashlib.sha256(beauty_path.read_bytes()).hexdigest() == before[beauty_path]
assert hashlib.sha256(zip_path.read_bytes()).hexdigest() == before[zip_path] and piz_path.is_file()
scene = dest / "routed-layout.hiplc"
hou.hipFile.save(str(scene), save_to_recent_files=False)
hou.hipFile.load(str(scene), suppress_save_prompt=True)
rop = hou.node("/out/routed_outputs")
assert len(output.plan(rop, hou.time())) == 3 and rop.evalParm("pass_count") == 3
assert not rop.evalParm("output_pass_folders")
# Resolve automatic names from the original base directory, once, per frame.
rop.setParms(dict(output_pass_folders=1, output_existing="overwrite", pass_path1="", pass_path3=""))
jobs = output.plan(rop, hou.time())
expected = [
    dest / "beauty/beauty.0001.exr",
    dest / "masksZIP/beauty.masksZIP.0001.exr",
    dest / "masksPIZ.0001.exr",
    dest / "N/beauty.N.0001.exr",
]
assert [Path(job["filename"]) for job in jobs] == expected, jobs
# Use the raw Houdini template to distinguish the frame field from camera,
# version and repeated numeric text without evaluating a different frame.
for template, pass_name in (
    ("Blender_Instancing.cam_2.$F4.exr", "Blender_Instancing.cam_2.masksZIP.0001.exr"),
    ("shot.0001.$F4.exr", "shot.0001.masksZIP.0001.exr"),
    ("shot.${F4}.exr", "shot.masksZIP.0001.exr"),
    ("shot.$F4.0001.exr", "shot.masksZIP.0001.0001.exr"),
    ("shot.0001.exr", "shot.0001.masksZIP.exr"),
):
    rop.parm("picture").set(str(dest / template))
    assert Path(output.plan(rop, hou.frameToTime(1))[1]["filename"]).name == pass_name
for template, frame, pass_name in (
    ("shot-$F4.exr", 1, "shot.masksZIP-0001.exr"),
    ("shot-$F4.exr", -1, "shot.masksZIP--001.exr"),
    ("shot.2.$FF.exr", 1, "shot.2.masksZIP.1.exr"),
    ("shot.2.$FF.exr", 1.25, "shot.2.masksZIP.1.25.exr"),
):
    rop.parm("picture").set(str(dest / template))
    assert Path(output.plan(rop, hou.frameToTime(frame))[1]["filename"]).name == pass_name
rop.parm("picture").set(str(dest / "beauty.$F4.exr"))
rop.render(frame_range=(1, 1))
folder_summary = rop.cachedUserData(final_render._summary_key)
assert folder_summary["completed_frames"] == folder_summary["created_sessions"] == 1, folder_summary
for path, codec in zip(expected, ("dwab", "zip", "piz", "zip")):
    image = oiio.ImageInput.open(str(path))
    assert image, oiio.geterror()
    assert image.spec().get_string_attribute("compression") == codec
    image.close()
assert [Path(job["filename"]) for job in output.plan(rop, hou.frameToTime(2))] == [
    path.with_name(path.name.replace(".0001.", ".0002.")) for path in expected
]
manual = dest / "chosen.exr"
assert Path(output.plan(rop, hou.time(), str(manual))[0]["filename"]) == dest / "beauty/chosen.exr"
assert Path(output.plan(rop, hou.time(), str(manual))[2]["filename"]) == dest / "masksPIZ.0001.exr"
# Keep the single multilayer mode, and allow explicit shared-file routing.
for index in (1, 2, 3):
    rop.parm("pass_separate" + str(index)).set(0)
rop.parm("output_compression").set("zip")
assert len(output.plan(rop, hou.time())) == 1
rop.render(frame_range=(1, 1))
image = oiio.ImageInput.open(str(expected[0]))
assert image, oiio.geterror()
assert all(name in image.spec().channelnames for name in ("R", "N.X", "masksZIP00.R", "masksPIZ00.R"))
image.close()
rop.setParms(dict(pass_separate1=1, pass_path1=str(expected[0]), pass_compression1="inherit"))
assert len(output.plan(rop, hou.time())) == 1
rop.parm("output_pass_folders").set(0)
assert Path(output.plan(rop, hou.time())[0]["filename"]) == beauty_path
rop.parm("output_pass_folders").set(1)
scene = dest / "folder-layout.hiplc"
hou.hipFile.save(str(scene), save_to_recent_files=False)
hou.hipFile.load(str(scene), suppress_save_prompt=True)
rop = hou.node("/out/routed_outputs")
assert rop.evalParm("output_pass_folders") and len(output.plan(rop, hou.time())) == 1
# Global separation applies to existing and newly added rows without mutating
# individual toggles. Verify precision menus for every builtin pass from HOM.
rop.setParms(
    dict(pass_count=0, output_separate_passes=1, output_compression="dwab", picture=str(dest / "global.$F4.exr"))
)
half_index = passes.add(rop, "emission", name="glowHalf", precision="half")
float_index = passes.add(rop, "emission", name="glowFloat", precision="float")
passes.add(rop, "normal")
passes.add(rop, "crypto_object", levels=2, compression="piz")
passes.add(rop, "ao")
passes.add(rop, "depth", enable=False, path="invalid-disabled-file.png")
assert all(not rop.evalParm("pass_separate" + str(i)) for i in range(1, 7))
assert (
    rop.evalParm("pass_compression1") == rop.evalParm("pass_compression3") == rop.evalParm("pass_compression5") == "zip"
)
assert rop.parm("pass_compression1").parmTemplate().menuType() == hou.menuType.Normal
assert "Automatic" not in rop.parm("pass_compression1").menuLabels()
assert rop.parm("output_precision").parmTemplate().menuType() == hou.menuType.Normal
assert not rop.parm("output_precision").isDisabled()
for descriptor in passes.catalogue():
    if descriptor["id"] == "combined":
        continue
    rop.parm("pass_type1").set(descriptor["id"])
    rop.updateParmStates()
    assert not rop.parm("pass_precision1").isHidden() and not rop.parm("pass_precision1").isDisabled(), descriptor
assert rop.parm("pass_data_precision1") is None
rop.parm("pass_type1").set("emission")
rop.updateParmStates()
assert rop.parm("pass_separate1").isDisabled()
assert not rop.parm("pass_precision1").isDisabled() and not rop.parm("pass_path1").isDisabled()
assert rop.parm("pass_precision1").parmTemplate().menuType() == hou.menuType.Normal
jobs = output.plan(rop, hou.time())
assert len(jobs) == 6, jobs
assert [Path(job["filename"]).parent.name for job in jobs] == [
    "beauty",
    "glowHalf",
    "glowFloat",
    "normal",
    "CryptoObject",
    "ao",
]
rop.render(frame_range=(1, 1))
global_summary = rop.cachedUserData(final_render._summary_key)
assert global_summary["completed_frames"] == global_summary["created_sessions"] == 1, global_summary
for job, precision, codec in zip(
    jobs, ("half", "half", "float", "float", "float", "float"), ("dwab", "zip", "zip", "zip", "piz", "zip")
):
    image = oiio.ImageInput.open(job["filename"])
    assert image, oiio.geterror()
    spec = image.spec()
    assert spec.get_string_attribute("compression") == codec
    assert all(str(fmt) == precision for fmt in spec.channelformats) or str(spec.format) == precision
    image.close()
# Every separate file has its own explicit codec. Main codec changes never
# modify the defaults; deliberate Use Main Output follows it without adapting.
for codec in sorted(output.CODECS):
    rop.parm("output_compression").set(codec)
    planned = output.plan(rop, hou.time())
    assert [job["options"]["compression"] for job in planned] == [codec, "zip", "zip", "zip", "piz", "zip"], planned
rop.setParms(dict(pass_compression1="inherit", pass_compression4="inherit", pass_compression5="inherit"))
for codec in sorted(output.CODECS):
    rop.parm("output_compression").set(codec)
    planned = output.plan(rop, hou.time())
    assert [job["options"]["compression"] for job in planned] == [codec, codec, "zip", "zip", codec, codec], planned
rop.parm("output_compression").set("dwab")
rop.render(frame_range=(1, 1))
for job, codec in zip(output.plan(rop, hou.time()), ("dwab", "dwab", "zip", "zip", "dwab", "dwab")):
    image = oiio.ImageInput.open(job["filename"])
    assert image, oiio.geterror()
    assert image.spec().get_string_attribute("compression") == codec
    image.close()
rop.parm("pass_compression3").set("dwab")
rop.parm("pass_compression5").set("dwab")
assert [job["options"]["compression"] for job in output.plan(rop, hou.time())] == [
    "dwab",
    "dwab",
    "zip",
    "dwab",
    "dwab",
    "dwab",
]
rop.render(frame_range=(1, 1))
for job in output.plan(rop, hou.time()):
    image = oiio.ImageInput.open(job["filename"])
    assert image, oiio.geterror()
    expected_codec = job["options"]["compression"]
    assert image.spec().get_string_attribute("compression") == expected_codec
    image.close()
rop.setParms(dict(pass_compression1="zip", pass_compression3="zip", pass_compression4="piz", pass_compression5="zip"))
assert len(output.plan(rop, hou.time())) == 6
rop.parm("output_separate_passes").set(0)
rop.parm("output_compression").set("zip")
rop.updateParmStates()
assert not rop.parm("pass_precision1").isDisabled() and rop.parm("pass_path1").isDisabled()
assert len(output.plan(rop, hou.time())) == 1
rop.parm("pass_separate1").set(1)
rop.updateParmStates()
assert not rop.parm("pass_precision1").isDisabled() and len(output.plan(rop, hou.time())) == 2
rop.parm("output_separate_passes").set(1)
scene = dest / "global-layout.hiplc"
hou.hipFile.save(str(scene), save_to_recent_files=False)
hou.hipFile.load(str(scene), suppress_save_prompt=True)
rop = hou.node("/out/routed_outputs")
assert rop.evalParm("output_separate_passes") and len(output.plan(rop, hou.time())) == 6
# Mixed channel precision works inside one ordinary multilayer EXR and also
# through the worker output packet. Half never changes the source frame/OCIO.
rop.setParms(
    dict(
        pass_count=0,
        output_separate_passes=0,
        output_precision="half",
        output_compression="zip",
        picture=str(dest / "precision.$F4.exr"),
    )
)
rows = [
    ("ao", "AO_Half", "half"),
    ("ao", "AO_Float", "float"),
    ("normal", "N_Half", "half"),
    ("depth", "Z_Float", "float"),
    ("crypto_object", "CryptoHalf", "half"),
    ("crypto_object", "CryptoFloat", "float"),
    ("emission", "EmissionHalf", "half"),
    ("emission", "EmissionFloat", "float"),
]
for kind, name, precision in rows:
    index = passes.add(rop, kind, name=name, precision=precision, levels=2)
    rop.updateParmStates()
    assert not rop.parm("pass_precision" + str(index)).isDisabled()
assert passes.entries(rop, hou.time(), output=True)[0]["precision"] == "half"
with ch.Session(device=device, width=96, height=64, samples=16, interactive=False) as session:
    frame = session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
    metadata = session.frame_info()["pass_metadata"]
    for separate in (False, True):
        rop.parm("output_separate_passes").set(int(separate))
        jobs = output.plan(rop, hou.time())
        assert len(jobs) == (9 if separate else 1), jobs
        output.write_outputs(session, jobs)
        for job in jobs:
            image = oiio.ImageInput.open(job["filename"])
            assert image, oiio.geterror()
            spec = image.spec()
            pixels = image.read_image(format=oiio.FLOAT)
            image.close()
            formats = (
                dict(zip(spec.channelnames, map(str, spec.channelformats)))
                if spec.channelformats
                else dict.fromkeys(spec.channelnames, str(spec.format))
            )
            for selected in job["options"]["passes"]:
                source = selected["source"]
                layer = selected["layer"]
                precision = selected["precision"]
                expected = "half" if precision in ("half", "inherit") else "float"
                standalone = len(job["options"]["passes"]) == 1 and not source.startswith("Crypto")
                components = metadata[source]["channels"]
                names = (
                    (["Y"] if len(components) == 1 else list("RGB") + components[3:])
                    if standalone
                    else [layer + "." + c if layer else c for c in components]
                )
                assert names and all(formats[name] == expected for name in names), (selected, formats)
                if source.startswith("Crypto"):
                    assert any(p.name.endswith("/name") and p.value == layer[:-2] for p in spec.extra_attribs)
                raw = frame["planes"][source]
                before = np.flipud(np.frombuffer(raw["pixels"], np.float32).reshape(64, 96, raw["channels"])).copy()
                actual = pixels[:, :, [spec.channelnames.index(name) for name in names]]
                if expected == "float":
                    assert np.array_equal(actual.view(np.uint32), before.view(np.uint32)), selected
                elif not source.startswith("Crypto"):
                    np.testing.assert_array_equal(actual, before.astype(np.float16).astype(np.float32))
    # Inherit means the exact main precision on raw data and Cryptomatte too.
    rop.setParms(dict(pass_precision1="inherit", pass_precision5="inherit"))
    for main_precision in ("half", "float"):
        rop.parm("output_precision").set(main_precision)
        jobs = output.plan(rop, hou.time())
        output.write_outputs(session, jobs)
        for job in (jobs[1], jobs[5]):
            image = oiio.ImageInput.open(job["filename"])
            assert image, oiio.geterror()
            spec = image.spec()
            image.close()
            assert str(spec.format) == main_precision or all(str(f) == main_precision for f in spec.channelformats)
    assert session.frame()["planes"]["CryptoObject00"]["pixels"] == frame["planes"]["CryptoObject00"]["pixels"]
    previous = Path(jobs[1]["filename"]).read_bytes()
    bad = dict(jobs[1]["options"], passes=[dict(source="ao", layer="AO", precision="invalid")])
    try:
        session.write_exr(jobs[1]["filename"], bad)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("Unknown channel precision accepted")
    assert Path(jobs[1]["filename"]).read_bytes() == previous
report = dict(
    result="passed",
    device=device,
    checks=[
        "One render/generation writes Beauty DWAB and Cryptomatte ZIP/PIZ",
        "Data grouped by file, layer aliases and float32 Cryptomatte IDs/manifests preserved",
        "No implicit guides in Beauty; no Cryptomatte metadata in Beauty",
        "Conflicting shared-file settings rejected without changing completed files; repair works",
        "Skip requires every output; missing outputs resume from one render while existing files remain unchanged",
        "Native list has relevant controls, no retired schema and survives HIP reload",
        "Pass Folders creates Beauty and layer folders, respects frame evaluation/custom paths and viewer filename overrides",
        "Folder mode retains single multilayer output and explicit shared-file routing; toggle survives HIP reload",
        "Global separation covers existing/new enabled rows, preserves individual toggles and survives HIP reload",
        "Precision menus are visible and editable for every pass type, for separate and shared files",
        "One global-separated render writes half and full float color with float32 data and Cryptomatte",
        "Every pass defaults explicitly to ZIP; changing the main codec does not alter separate codecs; no Automatic menu",
        "Use Main Output follows all ten codecs exactly for color, AO and Cryptomatte; explicit AO/Normal DWAB accepted",
        "Half/full float AO, normals, emission and Cryptomatte written independently within shared and separate EXRs; float data bit-exact, raw Half correctly rounded",
        "Main precision inheritance is literal for data and IDs; invalid precision preserves prior output and retained frame remains unchanged",
    ],
    summary=summary,
)
(dest / "houdini-pass-outputs-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=report["checks"])))
