# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import numpy as np
import OpenImageIO as oiio
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from volume_field_recovery_fixture import fixture, check, RECOVERY_MODES, MISSING_DENSITY

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
assert ch._native.probe_gridless_volume()
rop, edit, dest = fixture(device)
entry = registry.for_rop(rop, native_scene.configuration(rop))
events = [
    dict(
        check="Gridless density, mixed valid/missing headers, and repair through native capture/conversion; no loose particles"
    )
]
try:
    baseline = None
    for mode in RECOVERY_MODES:
        edit.parm("mode").set(mode)
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        pixels = check(frame, status, mode)
        if baseline is None:
            baseline = pixels
        if mode not in MISSING_DENSITY and mode != 12:
            assert np.array_equal(pixels, baseline), "Retained density changed"
        assert status["geometry_resources"] == status["object_resources"] == 1 + int(mode not in MISSING_DENSITY), (
            status
        )
        conversions = status["geometry_conversions"]
        entry.session.wait(entry.apply(dict(camera_path=rop.evalParm("camera")), hou.time()))
        assert entry.session.status()["geometry_conversions"] == conversions
        assert entry.session.status()["source_diagnostics"] == status["source_diagnostics"]
        events.append(dict(mode=mode, diagnostics=status["source_diagnostics"]))
    # The mesh in the same SOP must remain editable while the volume is omitted.
    edit.parm("mode").set(2)
    box = edit.parent().node("unaffected_mesh")
    box.parm("tx").set(1.2)
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(48, 80, 4)
    assert pixels[24, 64, 1] > 0.6 and pixels[24, 64, 0] < 0.2
    assert np.allclose(pixels[24, 70, :3], 1), "The mesh remained at its old position"
    assert entry.session.status()["object_resources"] == 1
    box.parm("tx").set(1.5)
    events.append(dict(check="Mesh moves while sparse density is unavailable"))
    # Five ordered output frames, including failures and repairs.
    for frame_number, mode in enumerate((0, 10, 0, 2, 0), 1):
        hou.setFrame(frame_number)
        edit.parm("mode").set(mode)
        entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        path = dest / f"recovery.{frame_number:04d}.exr"
        entry.session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        assert reader
        issues = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(issues) == (mode != 0), issues
    # Final rendering must use the same recovery policy without the IPR worker.
    with ch.Session(device=device, width=80, height=48, samples=256, execution="sync", interactive=False) as final:
        for mode in (0, 5, 6, 7, 8, 9, 10, 11, 12, 0):
            edit.parm("mode").set(mode)
            frame = final.wait(final.apply(native_scene.capture(rop), at_time=hou.time()))
            check(frame, final.status(), mode)
    events.append(dict(check="Direct final capture recovers and repairs"))
    hou.hipFile.save(str(dest / "volume-field-recovery.hiplc"))
finally:
    registry.close_all()
report = dict(result="passed", device=device, events=events, sequence_frames=5)
path = artifacts_root() / "stage3/materials/recovery/houdini-volume-field-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
