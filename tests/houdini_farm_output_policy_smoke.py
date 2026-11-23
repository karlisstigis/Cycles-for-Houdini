# SPDX-License-Identifier: Apache-2.0
"""Native writer collision races, validated resume contracts and strict IPC output."""

from support.paths import artifacts_root
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import threading
import time

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import output, passes, final_render
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = artifacts_root() / "stage3/farm-output-policy"
root.mkdir(parents=True, exist_ok=True)
dest = root / str(time.time_ns())
dest.mkdir()
checks = []


def rejected(call, text=""):
    try:
        call()
    except (RuntimeError, ValueError) as error:
        assert not text or text in str(error), (text, error)
        return str(error)
    raise AssertionError("Unsafe output was accepted")


def write_authored_exr(path, specs, pixels):
    writer = oiio.ImageOutput.create(str(path))
    assert writer.open(str(path), specs if len(specs) > 1 else specs[0]), writer.geterror()
    for index, (spec, data) in enumerate(zip(specs, pixels, strict=True)):
        if index:
            assert writer.open(str(path), spec, "AppendSubimage"), writer.geterror()
        assert writer.write_image(data), writer.geterror()
    assert writer.close(), writer.geterror()


def corrupt_depth(data):
    """Change the first ZIP byte in Depth after two Beauty chunks."""
    data = bytearray(data)
    cursor = 8
    for _ in range(2):
        while data[cursor]:
            for _ in range(2):
                cursor = data.index(0, cursor) + 1
            size = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4 + size
        cursor += 1
    assert data[cursor] == 0
    depth = struct.unpack_from("<Q", data, cursor + 1 + 2 * 8)[0]
    part, first_row, packed_size = struct.unpack_from("<iii", data, depth)
    assert (part, first_row) == (1, 0) and 0 < packed_size < 32 * 16 * 4
    data[depth + 12] ^= 0xFF
    return bytes(data)


# External level-0 tiles and multipart parts use the same native preflight as
# Render to Disk. Keep Beauty readable when damaging a later part's ZIP stream.
rng = np.random.default_rng(761)
beauty = oiio.ImageSpec(32, 24, 4, oiio.FLOAT)
beauty.channelnames = ("R", "G", "B", "A")
beauty_pixels = rng.random((24, 32, 4), dtype=np.float32)
beauty.attribute("compression", "none")
beauty.tile_width = beauty.tile_height = 16
beauty.tile_depth = 1
tiled = dest / "authored-tiled.exr"
write_authored_exr(tiled, [beauty], [beauty_pixels])
contract = dict(window=(0, 0, 32, 24, 0, 0, 32, 24), multipart=False, parts={"": list(beauty.channelnames)})
assert output.inspect_existing(tiled, contract) == []
tiled_bytes = tiled.read_bytes()[:-32]
tiled.write_bytes(tiled_bytes)
assert output.inspect_existing(tiled, contract, read_pixels=False) == []
rejected(lambda: output.inspect_existing(tiled, contract), "pixel data is unreadable")
assert tiled.read_bytes() == tiled_bytes

beauty.tile_width = beauty.tile_height = beauty.tile_depth = 0
depth = oiio.ImageSpec(32, 24, 1, oiio.FLOAT)
depth.channelnames = ("Z",)
for name, spec in (("Beauty", beauty), ("Depth", depth)):
    spec.attribute("oiio:subimagename", name)
    spec.attribute("compression", "zip")
depth_pixels = np.linspace(0, 1, 32 * 24, dtype=np.float32).reshape(24, 32, 1)
multipart_path = dest / "authored-multipart.exr"
write_authored_exr(multipart_path, [beauty, depth], [beauty_pixels, depth_pixels])
contract = dict(contract, multipart=True, parts={"Beauty": list(beauty.channelnames), "Depth": ["Z"]})
assert output.inspect_existing(multipart_path, contract) == []
damaged = corrupt_depth(multipart_path.read_bytes())
multipart_path.write_bytes(damaged)
reader = oiio.ImageInput.open(str(multipart_path))
assert reader is not None, oiio.geterror()
try:
    spec = reader.spec()
    assert spec.get_string_attribute("oiio:subimagename") == "Beauty"
    np.testing.assert_array_equal(
        reader.read_image(), beauty_pixels[..., [beauty.channelnames.index(c) for c in spec.channelnames]]
    )
    assert reader.seek_subimage(1, 0) and reader.spec().get_string_attribute("oiio:subimagename") == "Depth"
    assert reader.read_image() is None
    reader.geterror()
finally:
    reader.close()
assert output.inspect_existing(multipart_path, contract, read_pixels=False) == []
rejected(lambda: output.inspect_existing(multipart_path, contract), "pixel data is unreadable")
assert multipart_path.read_bytes() == damaged
checks.append("Skip rejects a truncated tile and a damaged later ZIP part whose Beauty remains exact")


with ch.Session(device=device, width=320, height=180, samples=1, interactive=False) as session:
    session.wait(session.calibration(), timeout=120)
    target = dest / "native.exr"
    session.write_exr(target)
    original = target.read_bytes()
    session.write_exr(target, dict(existing_files="skip"))
    assert target.read_bytes() == original
    truncated = dest / "native-truncated.exr"
    session.write_exr(truncated, dict(compression="none"))
    truncated_bytes = truncated.read_bytes()[:-32]
    truncated.write_bytes(truncated_bytes)
    reader = oiio.ImageInput.open(str(truncated))
    assert reader is not None, oiio.geterror()
    # The SDK's OIIO can return pixels for a shortened uncompressed chunk.
    # The writer must independently detect the extent beyond end of file.
    assert reader.format_name() == "openexr" and (reader.spec().width, reader.spec().height) == (320, 180)
    assert reader.close()
    rejected(lambda: session.write_exr(truncated, dict(existing_files="skip")), "pixel data is unreadable")
    assert truncated.read_bytes() == truncated_bytes
    rejected(lambda: session.write_exr(target, dict(existing_files="error")), "already exists")
    target.write_bytes(b"corrupt output")
    rejected(lambda: session.write_exr(target, dict(existing_files="skip")), "readable EXR")
    assert target.read_bytes() == b"corrupt output"
    session.write_exr(target, dict(existing_files="overwrite"))
    directory = dest / "directory.exr"
    directory.mkdir()
    rejected(lambda: session.write_exr(directory, dict(existing_files="skip")), "regular file")
    session.wait(session.apply(dict(crop=(0, 0, 160, 90))))
    rejected(lambda: session.write_exr(target, dict(existing_files="skip")), "dimensions")
    session.wait(session.apply(dict(crop=(0, 0, 320, 180))))
    selected = dict(passes=[dict(source="combined", layer=f"layer{i}") for i in range(32)])
    winner = dest / "winner.exr"
    session.write_exr(winner, selected)
    winner_bytes = winner.read_bytes()
    for policy, outcome, competing in (
        ("error", "exists", b"concurrent winner sentinel"),
        ("skip", "valid", winner_bytes),
        ("skip", "header", b"concurrent winner sentinel"),
        ("skip", "pixels", winner_bytes[:-32]),
    ):
        path = dest / f"race-{policy}-{outcome}.exr"
        errors = []

        def write():
            try:
                session.write_exr(path, dict(selected, existing_files=policy))
            except Exception as error:
                errors.append(str(error))

        worker = threading.Thread(target=write)
        worker.start()
        deadline = time.monotonic() + 20
        seen = False
        while worker.is_alive() and time.monotonic() < deadline:
            if list(dest.glob(".cycles-*.tmp.exr")):
                path.write_bytes(competing)
                seen = True
                break
            time.sleep(0.0005)
        worker.join(30)
        assert not worker.is_alive()
        assert seen, "Encoding completed before publication race could be exercised"
        assert path.read_bytes() == competing, (policy, errors)
        assert bool(errors) == (outcome != "valid"), (policy, errors)
        if outcome == "pixels":
            assert "pixel data is unreadable" in errors[0], errors
        assert not list(dest.glob(".cycles-*.tmp.exr"))
    checks.append(
        "Native no-replace publication preserves concurrent Error/Skip winners, validates Skip and retires staging"
    )
    session.wait(
        session.apply(
            dict(
                source_diagnostics=[
                    dict(
                        code="geometry.missing_source",
                        source="/obj/missing/OUT",
                        resource="missing",
                        message="Missing geometry",
                        action="Restore it",
                    )
                ]
            )
        )
    )
    strict = dest / "strict-native.exr"
    rejected(lambda: session.write_exr(strict, dict(fail_on_missing_inputs=True)), "geometry.missing_source")
    assert not strict.exists()
    session.write_exr(strict)
    fallback_winner = dest / "fallback-winner.exr"
    session.write_exr(fallback_winner, selected)
    fallback_bytes = fallback_winner.read_bytes()
    session.wait(session.apply(dict(source_diagnostics=[])))
    rejected(
        lambda: session.write_exr(strict, dict(existing_files="skip", fail_on_missing_inputs=True)),
        "contains missing render inputs",
    )
    # A fallback file arriving after encoding begins must also fail strict Skip.
    strict_race = dest / "strict-race.exr"
    race_errors = []

    def strict_write():
        try:
            session.write_exr(strict_race, dict(selected, existing_files="skip", fail_on_missing_inputs=True))
        except Exception as error:
            race_errors.append(str(error))

    worker = threading.Thread(target=strict_write)
    worker.start()
    seen = False
    deadline = time.monotonic() + 20
    while worker.is_alive() and time.monotonic() < deadline:
        if list(dest.glob(".cycles-*.tmp.exr")):
            strict_race.write_bytes(fallback_bytes)
            seen = True
            break
        time.sleep(0.0005)
    worker.join(30)
    assert seen and not worker.is_alive()
    assert race_errors and "geometry.missing_source" in race_errors[0], race_errors
    assert strict_race.read_bytes() == fallback_bytes
    external = dest / "external.exr"
    writer = oiio.ImageOutput.create(str(external))
    assert writer.open(str(external), oiio.ImageSpec(320, 180, 4, oiio.FLOAT))
    assert writer.write_image(np.zeros((180, 320, 4), np.float32))
    assert writer.close()
    primary = dict(passes=[dict(source="combined", layer="")], existing_files="skip")
    session.write_exr(external, primary)
    rejected(lambda: session.write_exr(external, dict(primary, fail_on_missing_inputs=True)), "uncertified")
    repaired = dest / "strict-repaired.exr"
    session.write_exr(repaired, dict(fail_on_missing_inputs=True))
    assert repaired.is_file()
    session.write_exr(repaired, dict(existing_files="skip", fail_on_missing_inputs=True))
    checks.append(
        "Strict native output blocks missing inputs and old/concurrent fallback pixels after scene repair; unknown external provenance fails closed while permissive Skip remains available"
    )

# Explicit external worker exercises OutputOptions wire serialization and writer acknowledgements.
with ch.Session(device=device, width=32, height=24, samples=1, interactive=False, execution="worker") as session:
    session.wait(session.calibration(), timeout=120)
    path = dest / "worker.exr"
    session.write_exr(path)
    original = path.read_bytes()
    rejected(lambda: session.write_exr(path, dict(existing_files="error")), "already exists")
    session.write_exr(path, dict(existing_files="skip"))
    assert path.read_bytes() == original
    broken = dest / "worker-truncated.exr"
    session.write_exr(broken, dict(compression="none"))
    truncated_bytes = broken.read_bytes()[:-32]
    broken.write_bytes(truncated_bytes)
    rejected(
        lambda: session.write_exr(broken, dict(existing_files="skip", fail_on_missing_inputs=True)),
        "pixel data is unreadable",
    )
    assert broken.read_bytes() == truncated_bytes
    session.wait(
        session.apply(
            dict(
                source_diagnostics=[
                    dict(
                        code="texture.unavailable",
                        source="/textures/worker-missing.exr",
                        resource="shader",
                        message="Missing input",
                        action="Restore it",
                    )
                ]
            )
        ),
        timeout=120,
    )
    strict = dest / "worker-strict.exr"
    rejected(lambda: session.write_exr(strict, dict(fail_on_missing_inputs=True)), "texture.unavailable")
    assert not strict.exists()
checks.append("External worker IPC retains Error/Skip/strict policies and reports publication failures")

geo = hou.node("/obj").createNode("geo", "farm_contract_subject", run_init_scripts=False)
box = geo.createNode("box")
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "farm_contract_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "farm_contract_render")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=geo.path(),
        camera=camera.path(),
        resx=32,
        resy=24,
        samples=1,
        render_threads=2,
        use_denoise=0,
        adaptive_sampling=0,
        picture=str(dest / "contract.$F4.exr"),
        output_existing="overwrite",
        output_precision="float",
    )
)
assert rop.parm("fail_on_missing_inputs").eval() == 0
passes.add(rop, "normal", name="N", compression="zip", precision="float")
passes.add(rop, "crypto_object", name="IDs", levels=4, compression="piz", precision="float")
passes.add(rop, "aov_color", source="CustomColor", name="ColorAOV")
passes.add(rop, "aov_value", source="CustomValue", name="ValueAOV")
passes.add(rop, "lightgroup", source="Group", name="GroupAOV")
for multipart, separate, crop_region in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 0, 1)):
    rop.setParms(
        dict(
            output_multipart=multipart,
            output_separate_passes=separate,
            render_region=crop_region,
            crop_to_region=crop_region,
            output_compression="dwab" if separate else "zip",
            output_precision="half" if separate else "float",
            picture=str(dest / f"contract-{multipart}-{separate}-{crop_region}.$F4.exr"),
        )
    )
    if crop_region:
        rop.parmTuple("render_region_bounds").set((0.1, 0.9, 0.2, 0.8))
    rop.render(frame_range=(1, 1))
    jobs = output.plan(rop, hou.time())
    if separate:
        assert Path(jobs[1]["filename"]).name == f"contract-{multipart}-{separate}-{crop_region}.N.0001.exr"
        for job, codec in zip(jobs[:3], ("dwab", "zip", "piz"), strict=True):
            reader = oiio.ImageInput.open(job["filename"])
            assert reader is not None, oiio.geterror()
            try:
                assert reader.spec().get_string_attribute("compression") == codec
                pixels = reader.read_image()
                assert pixels is not None and pixels.shape[:2] == (24, 32) and np.isfinite(pixels).all()
            finally:
                reader.close()
    contracts = output.expected_headers(rop, hou.time(), jobs)
    before = {job["filename"]: hashlib.sha256(Path(job["filename"]).read_bytes()).hexdigest() for job in jobs}
    for job, contract in zip(jobs, contracts):
        assert output.inspect_existing(job["filename"], contract) is not None
    rop.parm("output_existing").set("skip")
    rop.render(frame_range=(1, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["created_sessions"] == 0 and summary["skipped_frames"] == 1, summary
    assert all(hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest for name, digest in before.items())
    # Reject a partial/incompatible set before publication, preserving every other member.
    altered = deepcopy(contracts[0])
    name = next(iter(altered["parts"]))
    altered["parts"][name].append("missing.channel")
    rejected(lambda: output.inspect_existing(jobs[0]["filename"], altered), "channels or parts")
    rop.parm("output_existing").set("overwrite")
checks.append(
    "Pre-render Skip matches native ordinary/shared/separate/multipart/cropped headers, including Crypto, AOV and light group naming"
)
checks.append("Real DWAB Beauty and ZIP/PIZ data outputs decode and retain byte identity through Skip")
report = dict(result="passed", device=device, checks=checks, destination=str(dest))
(root / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
