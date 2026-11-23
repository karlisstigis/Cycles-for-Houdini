# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from field_motion_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
events = []


def apply():
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    return frame, entry.session.status()


try:
    for case in CASES:
        f.configure(case)
        for steps in (3, 5):
            f.rop.parm("motion_samples").set(steps)
            frame, status = apply()
            baseline = f.check(frame, status)
            events.append(dict(case=case, samples=steps, check="Analytic mean square 1/3 on mesh, points and hair"))
        for target in range(3):
            f.target.set(target)
            for broken in (1, 2, 0):
                f.edit(broken)
                frame, status = apply()
                f.check(frame, status, broken, baseline)
                events.append(dict(case=case, target=target, broken=broken, diagnostics=status["source_diagnostics"]))
        f.target.set(0)
        for offset in (-0.5, 0.5):
            f.rop.parm("shutteroffset").set(offset)
            frame, status = apply()
            f.check(frame, status)
        f.rop.parm("shutteroffset").set(0)
        frame, status = apply()
        f.check(frame, status, False, baseline)
        conversions = status["geometry_conversions"]
        f.camera.parm("tx").set(0.01)
        entry.session.wait(entry.apply(dict(camera_path=f.camera.path()), hou.time()))
        assert entry.session.status()["geometry_conversions"] == conversions
        f.camera.parm("tx").set(0)
    # Unused malformed shutter data is ignored. A genuine active field failure
    # stays local, and turning blur off suppresses shutter-only warnings.
    f.configure("scalar")
    f.deletes[0].parm("ptdel").set("paint")
    f.edit(1)
    frame, status = apply()
    assert not status["source_diagnostics"], status
    f.deletes[0].parm("ptdel").set("mask")
    frame, status = apply()
    assert status["source_diagnostics"]
    conversions = status["geometry_conversions"]
    before = status["source_diagnostics"]
    frame, status = apply()
    assert status["geometry_conversions"] == conversions and status["source_diagnostics"] == before
    # Repair an inactive-at-centre branch without touching the nominal output.
    f.deletes[0].parm("ptdel").set("")
    frame, status = apply()
    f.check(frame, status)
    assert status["geometry_conversions"] == conversions + 1, status
    f.deletes[0].parm("ptdel").set("mask")
    frame, status = apply()
    assert status["source_diagnostics"]
    # Rewire the inactive input while preserving the set of upstream nodes: the
    # old delete remains connected through the invalid-field branch.
    f.switches[0].setInput(1, f.animators[0])
    frame, status = apply()
    f.check(frame, status)
    f.switches[0].setInput(1, f.deletes[0])
    frame, status = apply()
    assert status["source_diagnostics"]
    events.append(
        dict(
            check="Inactive branch parameter/connection edits invalidate only the affected component; no-op capture retains warnings"
        )
    )
    entry.session.wait(entry.apply(dict(motion_blur=False), hou.time()))
    assert not entry.session.status()["source_diagnostics"]
    entry.session.wait(entry.apply(dict(motion_blur=True), hou.time()))
    assert entry.session.status()["source_diagnostics"]
    f.edit(0)
    # Repeated recoveries must reuse the warmed owned/device/shader populations.
    # These are renderer counters, not a physical GPU-memory certification.
    f.configure("vector")
    warm = []
    for n in range(64):
        f.edit(n % 2)
        frame, status = apply()
        f.check(frame, status, n % 2)
        warm.append(
            (status["owned_geometry_bytes"], status["device_allocation_bytes"], status["engine_shader_resources"])
        )
    for parity in (0, 1):
        stable = warm[16 + parity :: 2]
        assert len({(owned, shaders) for owned, device_bytes, shaders in stable}) == 1, warm
        # The mixed mesh/point/hair fixture has small non-monotonic engine
        # allocation changes. A separate 64-edit settled-counter probe measured
        # 2,304 bytes of variation. Keep an explicit 8 KiB regression budget;
        # do not describe this as an exact or physical-memory plateau.
        device_bytes = [values[1] for values in stable]
        assert max(device_bytes) - min(device_bytes) <= 8192, warm
    # Native final output and five actual ROP/SOHO frames retain animated fields
    # and report recoverable missing/invalid states without interrupting output.
    f.configure("scalar")
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1024,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal"],
    ) as final:
        for broken in (0, 1, 0):
            f.edit(broken)
            frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
            f.check(frame, final.status(), broken)
    f.control.setExpression("1 if int(hou.frame()+.5) in (2,4) else 0", hou.exprLanguage.Python)
    f.rop.parm("picture").set(str(f.dest / "fields.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    for n in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"fields.{n:04d}.exr"))
        assert reader
        spec = reader.spec()
        pixels = reader.read_image()
        reader.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (n in (2, 4)), (n, issues)
        if issues:
            assert all(issue["attribute"] == "mask" for issue in issues), issues
        assert np.isfinite(pixels).all() and pixels.max() > 0.1
    f.control.deleteAllKeyframes()
    f.edit(0)
    hou.setFrame(1)
    hou.hipFile.save(str(f.dest / "field-motion.hiplc"))
finally:
    registry.close_all()
report = dict(
    result="passed",
    device=device,
    events=events,
    analytic_routes=len(CASES),
    native_batch_frames=5,
    warm_tracked_resources=warm,
)
path = artifacts_root() / "stage3/materials/field-motion/houdini-field-motion-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
