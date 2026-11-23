# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from domain_recovery_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
events = []
try:
    for case in CASES:
        f.configure(case)
        baseline = None
        for broken in (False, True, False):
            f.edit(broken)
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
            status = entry.session.status()
            pixels = f.check(frame, status, broken, baseline if not broken else None)
            assert not status["rendering"]["adaptive_sampling"] and status["current_sample"] == 256, status
            if baseline is None:
                baseline = pixels
            conversions = status["geometry_conversions"]
            entry.session.wait(entry.apply(dict(camera_path=f.rop.evalParm("camera")), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            assert entry.session.status()["source_diagnostics"] == status["source_diagnostics"]
            events.append(dict(case=case, broken=broken, diagnostics=status["source_diagnostics"]))
    # Keep the adaptive recovery path covered separately from deterministic
    # full-frame parity. Edge pixels may converge at different sample counts.
    f.rop.setParms(dict(adaptive_sampling=1, ipr_adaptive_sampling=1))
    f.configure("reference_object")
    for broken in (False, True, False):
        f.edit(broken)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        status = entry.session.status()
        f.check(frame, status, broken)
        assert status["rendering"]["adaptive_sampling"] and frame["complete"] and status["current_sample"] > 0, status
        events.append(
            dict(case="reference_object", adaptive=True, broken=broken, diagnostics=status["source_diagnostics"])
        )
    f.rop.setParms(dict(adaptive_sampling=0, ipr_adaptive_sampling=0))
    f.configure("volume_bsdf")
    for n, broken in enumerate((False, True, False, True, False), 1):
        hou.setFrame(n)
        f.edit(broken)
        entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        path = f.dest / f"recovery.{n:04d}.exr"
        entry.session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        assert reader
        warnings = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(warnings) == broken
    with ch.Session(
        device=device,
        width=128,
        height=64,
        samples=256,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal"],
    ) as final:
        for case in CASES:
            f.configure(case)
            for broken in (True, False):
                f.edit(broken)
                frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
                f.check(frame, final.status(), broken)
    f.configure("surface_volume")
    hou.hipFile.save(str(f.dest / "domain-recovery.hiplc"))
finally:
    registry.close_all()
report = dict(result="passed", device=device, events=events, sequence_frames=5)
path = artifacts_root() / "stage3/materials/recovery/houdini-domain-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
