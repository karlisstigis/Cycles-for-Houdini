# SPDX-License-Identifier: Apache-2.0
"""One render and one atomic EXR with independent parts, settings and manifests."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, output, passes
from cycles_houdini.device_selection import set_devices
from multipart_fixture import read_parts, verify_parts

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
dest = artifacts_root() / "stage3/multipart"
dest.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "multipart_subject", run_init_scripts=False)
sop = geo.createNode("box")
sop.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "multipart_camera")
camera.parm("tz").set(5)
rop = hou.node("/out").createNode("cycles_ipr", "multipart_output")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="sop",
        soppath=sop.path(),
        camera=camera.path(),
        resx=96,
        resy=64,
        viewer_interactive=0,
        samples=8,
        adaptive_sampling=0,
        use_denoise=0,
        render_threads=2,
        use_material_assignments=0,
        material_kind="emission",
        emission_strength=1,
        picture=str(dest / "all.$F4.exr"),
        output_multipart=1,
        output_pass_folders=1,
        output_compression="dwab",
        output_precision="half",
        output_dwa_level=70,
    )
)
rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
passes.add(
    rop,
    "ao",
    name="AO",
    separate=True,
    path="ignored-invalid-path.png",
    compression="dwaa",
    dwa_level=30,
    precision="half",
)
passes.add(rop, "crypto_object", name="CryptoZIP", levels=6, compression="zip", precision="float")
passes.add(rop, "crypto_object", name="CryptoPIZ", levels=6, compression="piz", precision="float")
rop.updateParmStates()
assert not rop.parm("output_multipart").isDisabled()
assert rop.parm("pass_separate1").isDisabled() and rop.parm("pass_path1").isDisabled()
assert not rop.parm("pass_compression1").isDisabled() and not rop.parm("pass_precision1").isDisabled()
jobs = output.plan(rop, hou.time())
assert len(jobs) == 1 and jobs[0]["options"]["multipart"]
rop.render(frame_range=(1, 1))
summary = rop.cachedUserData(final_render._summary_key)
assert summary["completed_frames"] == summary["created_sessions"] == summary["last_generation"] == 1, summary
path = dest / "beauty/all.0001.exr"
parts = verify_parts(path)
assert abs(parts["Beauty"]["dwa"] - 70) < 0.01 and abs(parts["AO"]["dwa"] - 30) < 0.01
assert not any("denoising" in c for p in parts.values() for c in p["channels"])
np.testing.assert_allclose(
    parts["Beauty"]["pixels"][32, 48, [parts["Beauty"]["channels"].index(c) for c in "RGB"]],
    (0.2, 0.4, 0.8),
    atol=0.025,
    rtol=0,
)
assert rop.evalParm("pass_separate1") and rop.evalParm("pass_path1") == "ignored-invalid-path.png"
scene = dest / "multipart-layout.hiplc"
hou.hipFile.save(str(scene), save_to_recent_files=False)
hou.hipFile.load(str(scene), suppress_save_prompt=True)
rop = hou.node("/out/multipart_output")
assert rop.evalParm("output_multipart") and len(output.plan(rop, hou.time())) == 1
# Separate Files suppresses the stored multipart flag, then restoring it
# returns to a single file without modifying any per-pass setting.
rop.parm("output_separate_passes").set(1)
rop.updateParmStates()
assert rop.parm("output_multipart").isDisabled() and rop.evalParm("output_multipart")
try:
    output.plan(rop, hou.time())
except ValueError as error:
    assert ".exr" in str(error), error
else:
    raise AssertionError("A bad explicit separate path was ignored")
rop.parm("pass_path1").set("")
jobs = output.plan(rop, hou.time())
assert len(jobs) == 4 and all(not job["options"]["multipart"] for job in jobs)
rop.parm("output_separate_passes").set(0)
assert len(output.plan(rop, hou.time())) == 1
# Same retained generation: part edits and output conversion do not mutate raw
# channels. Numeric IDs stay outside OCIO; crop/window metadata is per part.
with ch.Session(device=device, width=96, height=64, samples=8, interactive=False) as session:
    frame = session.wait(session.apply(native_scene.capture(rop), at_time=hou.time()))
    rop.setParms(dict(output_compression="zip", pass_compression1="zip", pass_precision1="float"))
    raw_jobs = output.plan(rop, hou.time(), str(dest / "raw.exr"))
    output.write_outputs(session, raw_jobs)
    raw = read_parts(raw_jobs[0]["filename"])
    target = "ACEScct"
    assert target in ch.color_policy()["output_spaces"]
    rop.setParms(dict(output_color_override=1, output_color_space=target))
    converted = output.plan(rop, hou.time(), str(dest / "converted.exr"))
    output.write_outputs(session, converted)
    changed = read_parts(converted[0]["filename"])
    for name in ("AO", "CryptoZIP", "CryptoPIZ"):
        assert np.array_equal(raw[name]["pixels"].view(np.uint32), changed[name]["pixels"].view(np.uint32)), name
    assert not np.array_equal(raw["Beauty"]["pixels"], changed["Beauty"]["pixels"])
    rop.setParms(
        dict(output_color_override=0, output_compression="dwab", pass_compression1="dwaa", pass_precision1="half")
    )
    previous = path.read_bytes()
    bad = output.plan(rop, hou.time())[0]["options"]
    bad["passes"][1]["compression"] = "invalid"
    try:
        session.write_exr(str(path), bad)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("Invalid part codec was accepted")
    assert path.read_bytes() == previous
    # Duplicate part names are rejected before a valid file is touched.
    rop.parm("pass_name1").set("Beauty")
    try:
        output.plan(rop, hou.time())
    except ValueError as error:
        assert "Duplicate multipart" in str(error), error
    else:
        raise AssertionError("Duplicate multipart name was accepted")
    rop.parm("pass_name1").set("AO")
    output.write_outputs(session, output.plan(rop, hou.time()))
    verify_parts(path)
    assert session.frame()["planes"]["CryptoObject00"]["pixels"] == frame["planes"]["CryptoObject00"]["pixels"]
report = dict(
    result="passed",
    device=device,
    summary=summary,
    checks=[
        "One render/generation writes one multipart EXR: Beauty half/DWAB, AO half/DWAA and grouped Cryptomatte float ZIP/PIZ",
        "Independent DWA levels, exact ZIP/PIZ ID/coverage bits and per-part renamed manifest metadata",
        "UI mode precedence, ignored/preserved individual paths/settings and HIP round-trip",
        "Output conversion changes Beauty while raw AO and IDs remain bit-exact; retained frame unchanged",
        "Invalid codec and duplicate part names preserve completed output; repair works",
    ],
)
(dest / "houdini-multipart-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
